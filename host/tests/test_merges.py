"""`merges.py` — the pure half of review and merge.

The argv are pinned by shape (every one starts with `work_record._git`'s head,
`-d` never `-D`, `--ff-only`, `--no-ff` with `-m`), the parsers by real output,
and the git behaviour the daemon leans on by a real repository under
`tmp_path`: a detached `merge --no-ff` carries the history's subject and two
parents, `--ff-only` over a modified file the merge changes leaves bytes and
HEAD alone, a stale `update-ref` moves nothing, and `branch -d` refuses an
unmerged branch and succeeds after the fast-forward
(`docs/card-worktrees.md`, *Review and merge*).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from dark_army_daemon import merges, work_record, worktrees

HEAD = work_record._git("/r")


def _run(argv, check=True):
    done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=merges.merge_env())
    if check:
        assert done.returncode == 0, done.stderr
    return done


def _git(root, *args, check=True):
    done = subprocess.run(["git", "-C", str(root), *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=merges.merge_env())
    if check:
        assert done.returncode == 0, done.stderr
    return done.stdout.decode()


# --- the argv --------------------------------------------------------------------


def _every_argv():
    r, p = "/r", "/p"
    return {
        "head_branch": merges.argv_head_branch(r),
        "tip": merges.argv_tip(r, "refs/heads/main"),
        "merge_base": merges.argv_merge_base(r, "a", "b"),
        "ahead_behind": merges.argv_ahead_behind(r, "main", "card/x"),
        "log": merges.argv_log(r, "a", "b"),
        "numstat": merges.argv_numstat_range(r, "a", "b"),
        "file_diff": merges.argv_range_file_diff(r, "a", "b", "f.py"),
        "detach": merges.argv_checkout_detach(p, "abc"),
        "branch": merges.argv_checkout_branch(p, "card/x"),
        "merge": merges.argv_merge(p, "card/x", "Merge card/x: t"),
        "abort": merges.argv_merge_abort(p),
        "unmerged": merges.argv_unmerged(p),
        "status": merges.argv_status(p),
        "incoming": merges.argv_incoming(r, "a", "b"),
        "ff": merges.argv_ff(r, "abc"),
        "update_ref": merges.argv_update_ref(r, "main", "new", "old"),
        "delete": merges.argv_branch_delete(r, "card/x"),
        "git_path": merges.argv_git_path(p, "MERGE_HEAD", "rebase-merge"),
    }


def test_every_argv_starts_with_the_shared_git_head():
    for name, argv in _every_argv().items():
        assert argv[:4] == HEAD[:4], name
        assert "-C" in argv[:6], name
        assert argv[0] == "git" and "--no-optional-locks" in argv, name


def test_the_delete_is_the_safe_one_and_nothing_pushes_or_forces():
    argv = merges.argv_branch_delete("/r", "card/x")
    assert "-d" in argv and "-D" not in argv
    for name, argv in _every_argv().items():
        assert "push" not in argv, name
        assert "--force" not in argv and "-f" not in argv, name
        assert "-D" not in argv, name
        assert "reset" not in argv and "rebase" not in argv, name


def test_the_merge_is_always_a_commit_with_the_given_subject():
    argv = merges.argv_merge("/p", "card/x", "Merge card/x: t")
    assert "--no-ff" in argv and "-m" in argv
    assert argv[argv.index("-m") + 1] == "Merge card/x: t"
    assert argv[-1] == "card/x"


def test_the_trunk_moves_only_forward_or_from_its_old_value():
    assert "--ff-only" in merges.argv_ff("/r", "abc")
    argv = merges.argv_update_ref("/r", "main", "new", "old")
    assert argv[-3:] == ["refs/heads/main", "new", "old"]


def test_merge_env_keeps_git_from_opening_an_editor():
    env = merges.merge_env({"PATH": "/usr/bin"})
    assert env["GIT_EDITOR"] == "true"
    assert env["GIT_MERGE_AUTOEDIT"] == "no"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_merges_py_names_no_subprocess_and_no_forbidden_flag():
    text = open(merges.__file__).read()
    assert "import subprocess" not in text
    assert text.count('"--ff-only"') == 1
    assert text.count('"--no-ff"') == 1
    for needle in ('"-D"', '"--force"', '"push"'):
        assert needle not in text, needle


# --- the names -------------------------------------------------------------------


def test_merge_subject_matches_the_history_and_clamps():
    card = {"id": "d5730b78-aaaa", "title": "  REVIEW   and\nMERGE  "}
    assert merges.merge_subject(card) == "Merge card/d5730b78: REVIEW and MERGE"
    long = merges.merge_subject({"id": "abcdef12", "title": "x" * 300})
    assert long.startswith("Merge card/abcdef12: ")
    assert len(long) <= len("Merge card/abcdef12: ") + merges.MAX_TITLE_CHARS
    assert merges.merge_subject({"id": "abcdef12", "title": ""}) \
        == "Merge card/abcdef12"


def test_merge_log_path_sits_beside_the_setup_log():
    path = merges.merge_log_path("/r", "d5730b78-aaaa")
    assert path == "/r/.worktrees/card-d5730b78.merge.log"
    assert os.path.dirname(path) == os.path.dirname(
        worktrees.setup_log_path("/r", "d5730b78-aaaa"))


def test_the_states_and_the_fix_states():
    assert merges.STATES == ("", "blocked", "conflict", "checks_failed",
                             "merged")
    assert set(merges.FIX_STATES) <= set(merges.STATES)
    assert merges.VERDICTS == ("", "ship", "stop")


# --- the parsers -----------------------------------------------------------------


def test_trunk_from():
    assert merges.trunk_from(b"origin/main\n") == "main"
    assert merges.trunk_from("origin/release/1") == "release/1"
    assert merges.trunk_from("main") == "main"
    assert merges.trunk_from("") == ""
    assert merges.trunk_from("a\nb") == ""


def test_is_tip():
    assert merges.is_tip("a" * 40)
    assert merges.is_tip("a" * 64)
    assert not merges.is_tip("A" * 40)
    assert not merges.is_tip("a" * 8)
    assert not merges.is_tip("")
    assert not merges.is_tip(None)


def test_parse_status_paths_modification_untracked_and_rename():
    data = (b" M a.txt\0?? new.txt\0R  moved.txt\0old.txt\0!! ignored\0"
            b"A  added.txt\0")
    assert merges.parse_status_paths(data) == [
        "a.txt", "new.txt", "moved.txt", "old.txt", "added.txt"]
    assert merges.parse_status_paths(b"") == []


def test_overlap_is_sorted_and_names_folders_that_meet_files():
    assert merges.overlap(["b", "a", "c"], ["a", "b", "z"]) == ["a", "b"]
    assert merges.overlap(["x"], ["y"]) == []
    assert merges.overlap(["dir"], ["dir/inner.txt"]) == ["dir"]
    assert merges.overlap(["dir/inner.txt"], ["dir"]) == ["dir/inner.txt"]
    assert merges.overlap([], ["a"]) == []


def test_listed_says_and_n_more():
    names = [f"f{i}" for i in range(merges.MAX_LISTED_PATHS + 3)]
    words = merges.listed(names)
    assert words.endswith("and 3 more")
    assert words.count(",") == merges.MAX_LISTED_PATHS - 1
    assert merges.listed(["a", "b"]) == "a, b"


def test_parse_worktree_branches_reads_names_not_counts():
    text = ("worktree /r\nHEAD abc\nbranch refs/heads/main\n\n"
            "worktree /private/tmp/baseline\nHEAD def\ndetached\n\n"
            "worktree /r/.worktrees/card-x\nHEAD 123\n"
            "branch refs/heads/card/x-y\n")
    got = merges.parse_worktree_branches(text)
    assert got[os.path.realpath("/r")] == "main"
    assert got[os.path.realpath("/private/tmp/baseline")] == ""
    assert got[os.path.realpath("/r/.worktrees/card-x")] == "card/x-y"


def test_parse_log_ahead_behind_and_caps():
    one = "a" * 40 + "\x1fAda\x1f1700000000\x1fFirst\x1e\n"
    two = "b" * 40 + "\x1fBob\x1f1700000100\x1fSecond\x1e\n"
    commits, cut = merges.parse_log(one + two)
    assert [c["subject"] for c in commits] == ["First", "Second"]
    assert commits[0] == {"sha8": "aaaaaaaa", "author": "Ada",
                          "at": 1700000000, "subject": "First"}
    assert cut is False
    many = one * (merges.MAX_COMMITS + 1)
    commits, cut = merges.parse_log(many)
    assert len(commits) == merges.MAX_COMMITS and cut is True
    assert merges.parse_ahead_behind("3\t2\n") == (3, 2)
    assert merges.parse_ahead_behind("garbage") == (0, 0)


def test_parse_numstat_z_counts_binaries_and_caps():
    data = b"3\t1\ta.py\0-\t-\timg.png\0notnumbers\tx\tz\0"
    rows, truncated, total = merges.parse_numstat_z(data)
    assert rows == [
        {"path": "a.py", "added": 3, "removed": 1, "binary": False},
        {"path": "img.png", "added": 0, "removed": 0, "binary": True}]
    assert truncated is False and total == 2
    big = b"".join(b"1\t0\tf%d\0" % i for i in range(work_record.MAX_FILES + 5))
    rows, truncated, total = merges.parse_numstat_z(big)
    assert len(rows) == work_record.MAX_FILES
    assert truncated is True and total == work_record.MAX_FILES + 5


@pytest.mark.parametrize("text,want", [
    ("VERDICT: SHIP\n\nlooks good", "ship"),
    ("VERDICT: STOP — it deletes the data", "stop"),
    ("verdict: stop", "stop"),
    ("\n\n  VERDICT:   Ship", "ship"),
    ("Looks fine.\nVERDICT: SHIP", ""),
    ("VERDICT: MAYBE", ""),
    ("VERDICTS: SHIP", ""),
    ("", ""),
])
def test_parse_verdict_reads_the_first_non_empty_line_only(text, want):
    assert merges.parse_verdict(text) == want


def test_review_current_and_notes():
    tip = "c" * 40
    assert merges.review_current(tip, tip)
    assert not merges.review_current(tip, "d" * 40)
    assert not merges.review_current("", "")
    assert "conflicts in a.py, b.py" in merges.conflict_note("main", ["a.py", "b.py"])
    note = merges.merged_note("main", "e" * 40, unchecked=True, folder_kept=True)
    assert note.startswith("Merged into main as eeeeeeee")
    assert merges.MERGED_UNCHECKED_NOTE in note
    assert merges.FOLDER_KEPT_SUFFIX in note and merges.BRANCH_KEPT_SUFFIX in note


def test_the_closed_key_sets_are_tuples_of_strings():
    for keys in (merges.CHANGES_KEYS, merges.COMMIT_KEYS, merges.FILE_ROW_KEYS,
                 merges.DIFF_KEYS, merges.REVIEW_KEYS):
        assert keys and all(isinstance(k, str) for k in keys)
    for key in ("branch_tip", "merge_offered", "merge_refusal", "review"):
        assert key in merges.CHANGES_KEYS


# --- a real repository through the argv ---------------------------------------


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("one\n")
    (root / "b.txt").write_text("keep\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "first")
    return str(root)


def _branch_with_commit(repo, name="card/x-t", file="a.txt", text="two\n"):
    _git(repo, "branch", name)
    wt = os.path.join(repo, ".worktrees", "card-x")
    _git(repo, "worktree", "add", "-q", wt, name)
    with open(os.path.join(wt, file), "w") as fh:
        fh.write(text)
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "work")
    return wt


def test_a_detached_merge_has_the_subject_and_both_parents(repo):
    wt = _branch_with_commit(repo, file="new.txt", text="n\n")
    base = _git(repo, "rev-parse", "main").strip()
    tip = _git(repo, "rev-parse", "card/x-t").strip()
    # The trunk moves meanwhile.
    (open(os.path.join(repo, "b.txt"), "w")).write("moved\n")
    _git(repo, "commit", "-q", "-am", "main moved")
    base = _git(repo, "rev-parse", "main").strip()
    _git(wt, "checkout", "-q", "--detach", base)
    subject = merges.merge_subject({"id": "d5730b78-x", "title": "do it"})
    _run(merges.argv_merge(wt, tip, subject))
    assert _git(wt, "log", "-1", "--format=%s").strip() == subject
    parents = _git(wt, "rev-list", "--parents", "-1", "HEAD").split()
    assert parents[1:] == [base, tip]
    # The main checkout never held it until the move.
    assert _git(repo, "rev-parse", "main").strip() == base


def test_a_conflict_is_listed_and_the_abort_leaves_the_folder_clean(repo):
    wt = _branch_with_commit(repo, text="branch\n")
    (open(os.path.join(repo, "a.txt"), "w")).write("main\n")
    _git(repo, "commit", "-q", "-am", "main edit")
    base = _git(repo, "rev-parse", "main").strip()
    tip = _git(repo, "rev-parse", "card/x-t").strip()
    _git(wt, "checkout", "-q", "--detach", base)
    done = subprocess.run(merges.argv_merge(wt, tip, "Merge card/x: t"),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=merges.merge_env())
    assert done.returncode != 0
    listed = _run(merges.argv_unmerged(wt)).stdout
    assert merges.parse_paths_z(listed) == ["a.txt"]
    _run(merges.argv_merge_abort(wt))
    assert _run(merges.argv_status(wt)).stdout == b""
    _run(merges.argv_checkout_branch(wt, "card/x-t"))
    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == "card/x-t"


def test_ff_only_over_a_modified_file_the_merge_changes_leaves_bytes_and_head(repo):
    wt = _branch_with_commit(repo)
    base = _git(repo, "rev-parse", "main").strip()
    tip = _git(repo, "rev-parse", "card/x-t").strip()
    _git(wt, "checkout", "-q", "--detach", base)
    _run(merges.argv_merge(wt, tip, "Merge card/x: t"))
    merge_tip = _git(wt, "rev-parse", "HEAD").strip()
    (open(os.path.join(repo, "a.txt"), "w")).write("unsaved\n")
    incoming = merges.parse_paths_z(
        _run(merges.argv_incoming(repo, base, merge_tip)).stdout)
    dirty = merges.parse_status_paths(_run(merges.argv_status(repo)).stdout)
    assert merges.overlap(dirty, incoming) == ["a.txt"]
    done = subprocess.run(merges.argv_ff(repo, merge_tip),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=merges.merge_env())
    assert done.returncode != 0
    assert open(os.path.join(repo, "a.txt")).read() == "unsaved\n"
    assert _git(repo, "rev-parse", "main").strip() == base


def test_ff_only_moves_main_and_leaves_an_untouched_dirty_file_alone(repo):
    wt = _branch_with_commit(repo)
    base = _git(repo, "rev-parse", "main").strip()
    tip = _git(repo, "rev-parse", "card/x-t").strip()
    _git(wt, "checkout", "-q", "--detach", base)
    _run(merges.argv_merge(wt, tip, "Merge card/x: t"))
    merge_tip = _git(wt, "rev-parse", "HEAD").strip()
    (open(os.path.join(repo, "b.txt"), "w")).write("unsaved b\n")
    (open(os.path.join(repo, "loose.txt"), "w")).write("untracked\n")
    _run(merges.argv_ff(repo, merge_tip))
    assert _git(repo, "rev-parse", "main").strip() == merge_tip
    assert open(os.path.join(repo, "b.txt")).read() == "unsaved b\n"
    assert open(os.path.join(repo, "loose.txt")).read() == "untracked\n"
    assert open(os.path.join(repo, "a.txt")).read() == "two\n"


def test_a_stale_update_ref_moves_nothing_and_a_fresh_one_moves(repo):
    wt = _branch_with_commit(repo)
    base = _git(repo, "rev-parse", "main").strip()
    tip = _git(repo, "rev-parse", "card/x-t").strip()
    stale = subprocess.run(
        merges.argv_update_ref(repo, "main", tip, "0" * 40),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=merges.merge_env())
    assert stale.returncode != 0
    assert _git(repo, "rev-parse", "main").strip() == base
    _run(merges.argv_update_ref(repo, "main", tip, base))
    assert _git(repo, "rev-parse", "main").strip() == tip
    assert wt  # the folder is untouched by the pointer move


def test_branch_d_refuses_unmerged_and_succeeds_after_the_ff(repo):
    wt = _branch_with_commit(repo)
    _git(wt, "checkout", "-q", "--detach")
    refused = subprocess.run(merges.argv_branch_delete(repo, "card/x-t"),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=merges.merge_env())
    assert refused.returncode != 0
    tip = _git(repo, "rev-parse", "card/x-t").strip()
    _run(merges.argv_ff(repo, tip))
    _run(merges.argv_branch_delete(repo, "card/x-t"))
    assert "card/x-t" not in _git(repo, "branch", "--list")


def test_log_numstat_and_ahead_behind_read_a_real_branch(repo):
    _branch_with_commit(repo, file="a.txt", text="two\nthree\n")
    base = _run(merges.argv_tip(repo, "refs/heads/main")).stdout.decode().strip()
    tip = _run(merges.argv_tip(repo, "refs/heads/card/x-t")).stdout.decode().strip()
    mb = _run(merges.argv_merge_base(repo, base, tip)).stdout.decode().strip()
    assert mb == base
    assert merges.parse_ahead_behind(
        _run(merges.argv_ahead_behind(repo, base, tip)).stdout) == (0, 1)
    commits, cut = merges.parse_log(_run(merges.argv_log(repo, mb, tip)).stdout)
    assert [c["subject"] for c in commits] == ["work"] and cut is False
    rows, _t, total = merges.parse_numstat_z(
        _run(merges.argv_numstat_range(repo, mb, tip)).stdout)
    assert rows == [{"path": "a.txt", "added": 2, "removed": 1, "binary": False}]
    assert total == 1
    text = _run(merges.argv_range_file_diff(repo, mb, tip, "a.txt")).stdout
    assert b"+three" in text


def test_git_path_names_the_marker_places(repo):
    out = _run(merges.argv_git_path(repo, "MERGE_HEAD", "rebase-merge")).stdout
    lines = out.decode().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("MERGE_HEAD") and lines[1].endswith("rebase-merge")
