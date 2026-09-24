"""The state folder is private: `ensure_state_dir` narrows it and the files
that hold something, on an interval, never raising."""

import dark_army_daemon.paths as paths


def _point_paths_at(monkeypatch, tmp_path):
    """Redirect the state dir to a temp location."""
    state = tmp_path / ".dark-army"
    monkeypatch.setattr(paths, "STATE_DIR", state)
    return state


def _rearm(monkeypatch):
    """The narrowing runs on an interval; a test wants it now."""
    monkeypatch.setattr(paths, "_restricted_at", 0.0)


def test_the_state_dir_is_private(monkeypatch, tmp_path):
    state = _point_paths_at(monkeypatch, tmp_path)
    _rearm(monkeypatch)
    paths.ensure_state_dir()
    assert (state.stat().st_mode & 0o777) == 0o700


def test_the_hook_pid_cache_dir_is_narrowed(monkeypatch, tmp_path):
    """The hook handler's memo directory is created by the script, not the
    daemon, so it may arrive with any mode; the periodic pass narrows it."""
    state = _point_paths_at(monkeypatch, tmp_path)
    cache = state / "hook-pids"
    monkeypatch.setattr(paths, "HOOK_PID_CACHE_DIR", cache)
    cache.mkdir(parents=True)
    cache.chmod(0o777)
    _rearm(monkeypatch)

    paths.ensure_state_dir()

    assert (cache.stat().st_mode & 0o777) == 0o700


def test_the_files_that_hold_something_are_narrowed(monkeypatch, tmp_path):
    """`history.db` is the one that matters: every project path, session title,
    model and cost this machine has ever run, and it shipped 0644."""
    state = _point_paths_at(monkeypatch, tmp_path)
    state.mkdir(parents=True)
    for name in ("history.db", "sessions.json", "titles.json", "identities.json"):
        (state / name).write_text("x")
        (state / name).chmod(0o644)
    _rearm(monkeypatch)

    paths.ensure_state_dir()

    for name in ("history.db", "sessions.json", "titles.json", "identities.json"):
        assert (state / name).stat().st_mode & 0o777 == 0o600, name


def test_a_file_created_later_is_narrowed_on_the_next_sweep(monkeypatch, tmp_path):
    """SQLite creates history.db on first connect, 0644, long after the first
    call here. A once-per-process latch would narrow everything except it."""
    state = _point_paths_at(monkeypatch, tmp_path)
    _rearm(monkeypatch)
    paths.ensure_state_dir()

    (state / "history.db").write_text("x")
    (state / "history.db").chmod(0o644)
    _rearm(monkeypatch)
    paths.ensure_state_dir()

    assert (state / "history.db").stat().st_mode & 0o777 == 0o600


def test_the_board_is_narrowed_on_the_re_check_too(monkeypatch, tmp_path):
    """`board.db` is the second SQLite file here and it repeats `history.db`'s
    exact trap: created 0644 on first connect, long after `ensure_state_dir`
    first ran. It holds every project path on the machine and the full text of
    every instruction anybody queued, so a miss here ships all of that
    world-readable."""
    state = _point_paths_at(monkeypatch, tmp_path)
    _rearm(monkeypatch)
    paths.ensure_state_dir()

    for name in ("board.db", "board.db-wal", "board.db-shm"):
        (state / name).write_text("x")
        (state / name).chmod(0o644)
    _rearm(monkeypatch)
    paths.ensure_state_dir()

    for name in ("board.db", "board.db-wal", "board.db-shm"):
        assert (state / name).stat().st_mode & 0o777 == 0o600, name


def test_an_unsettable_permission_does_not_stop_startup(monkeypatch, tmp_path):
    """Never raises: a permission we could not set is worth a log line, not a
    daemon that will not start."""
    state = _point_paths_at(monkeypatch, tmp_path)
    state.mkdir(parents=True)
    (state / "history.db").write_text("x")

    def boom(*_a, **_k):
        raise OSError("nope")

    monkeypatch.setattr(paths.Path, "chmod", boom)
    _rearm(monkeypatch)
    assert paths.ensure_state_dir() == state
