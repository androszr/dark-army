"""The one destructive operation in the daemon, so the tests are mostly about
what it refuses to do. The id reaching `delete_job` came off an HTTP request and
names a directory that is about to be handed to `shutil.rmtree` — every case
below is a way that could have gone wrong."""

import pytest

from dark_army_daemon import jobs_store
from dark_army_daemon.jobs_store import JobDeleteError, delete_job, job_dir


def _job(root, short_id="b3bbde6d", marker=True):
    """A job record shaped like the real thing: state.json plus the debris."""
    d = root / short_id
    (d / "tmp").mkdir(parents=True)
    if marker:
        (d / "state.json").write_text('{"state":"blocked"}')
    (d / "timeline.jsonl").write_text('{"t":1}\n')
    return d


def test_deletes_a_job_record(tmp_path):
    d = _job(tmp_path)
    assert delete_job("b3bbde6d", root=tmp_path) is True
    assert not d.exists()


def test_already_gone_is_not_an_error(tmp_path):
    """The panel can be seconds stale and two clicks can race. Settling quietly
    beats reporting a failure for work that is, by then, done."""
    assert delete_job("b3bbde6d", root=tmp_path) is False


def test_leaves_its_siblings_alone(tmp_path):
    _job(tmp_path, "aaaaaaaa")
    keep = _job(tmp_path, "bbbbbbbb")
    delete_job("aaaaaaaa", root=tmp_path)
    assert (keep / "state.json").exists()


# --- Refusals ----------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "..", ".", "", "../..", "a/b", "/etc", "~", "./x", "..%2F..", "-rf",
    "b3bbde6d/../../..", "\x00", "a" * 200,
    # `$` in a Python regex also matches before a trailing newline, so a
    # whitelist anchored with it would have accepted this one.
    "b3bbde6d\n", "b3bbde6d\n../..",
])
def test_refuses_anything_that_is_not_a_bare_id(tmp_path, bad):
    with pytest.raises(JobDeleteError):
        delete_job(bad, root=tmp_path)


def test_traversal_cannot_reach_a_sibling_of_the_jobs_dir(tmp_path):
    """The case the whitelist exists for: `jobs/../sessions` is a real directory
    and a purely textual check would have let it through."""
    root = tmp_path / "jobs"
    root.mkdir()
    victim = tmp_path / "sessions"
    victim.mkdir()
    (victim / "keep.json").write_text("{}")

    with pytest.raises(JobDeleteError):
        delete_job("../sessions", root=root)
    assert (victim / "keep.json").exists()


def test_refuses_a_symlink_even_when_it_points_at_a_valid_job(tmp_path):
    """A symlink resolves out of the jobs directory, so deleting it would delete
    whatever it aims at — and rmtree on the link target is not what "retire this
    job" means."""
    root = tmp_path / "jobs"
    root.mkdir()
    real = _job(tmp_path, "real0000")     # outside `root`, reachable only via the link
    (root / "link0000").symlink_to(real, target_is_directory=True)

    with pytest.raises(JobDeleteError):
        delete_job("link0000", root=root)
    assert (real / "state.json").exists()


def test_refuses_a_directory_that_is_not_a_job_record(tmp_path):
    """No state.json means we cannot positively identify it as a job. A mistyped
    or repurposed id must delete nothing."""
    d = tmp_path / "notajob"
    (d / "important").mkdir(parents=True)
    with pytest.raises(JobDeleteError, match="state.json"):
        delete_job("notajob", root=tmp_path)
    assert (d / "important").exists()


def test_refuses_the_real_file_that_lives_beside_the_jobs(tmp_path):
    """`pins.json` genuinely sits in ~/.claude/jobs/. It is refused on its name —
    ids carry no dots — before anything looks at the filesystem at all."""
    (tmp_path / "pins.json").write_text("[]")
    with pytest.raises(JobDeleteError, match="not a valid job id"):
        delete_job("pins.json", root=tmp_path)
    assert (tmp_path / "pins.json").read_text() == "[]"


def test_refuses_a_file_with_an_id_shaped_name(tmp_path):
    (tmp_path / "b3bbde6d").write_text("not a directory")
    with pytest.raises(JobDeleteError, match="not a directory"):
        delete_job("b3bbde6d", root=tmp_path)
    assert (tmp_path / "b3bbde6d").exists()


def test_non_string_ids_are_refused_rather_than_crashing(tmp_path):
    for bad in (None, 7, ["b3bbde6d"], {"id": "x"}):
        with pytest.raises(JobDeleteError):
            delete_job(bad, root=tmp_path)


def test_job_dir_resolves_inside_the_default_root():
    """The default root is the real ~/.claude/jobs — asserted so a refactor that
    repoints it has to say so here."""
    assert jobs_store.JOBS_DIR.name == "jobs"
    assert jobs_store.JOBS_DIR.parent.name == ".claude"


def test_rmtree_failure_becomes_a_refusal_not_a_traceback(tmp_path, monkeypatch):
    """An OSError has to reach the user as a message: this runs behind an HTTP
    handler that has to answer either way."""
    _job(tmp_path)
    monkeypatch.setattr(jobs_store.shutil, "rmtree",
                        lambda p: (_ for _ in ()).throw(OSError("read-only fs")))
    with pytest.raises(JobDeleteError, match="read-only fs"):
        delete_job("b3bbde6d", root=tmp_path)


def test_job_dir_returns_the_resolved_directory(tmp_path):
    d = _job(tmp_path)
    assert job_dir("b3bbde6d", root=tmp_path) == d.resolve()
