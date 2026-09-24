# host/tests/test_self_restart.py
"""Dark Army picking itself up when its background half dies.

Two halves, both without a menu bar or a real banner: the ledger and the three
pure decisions (`self_restart`), and the app-level decisions driven through
`test_menubar.py`'s `_bare_app()` seam — `object.__new__(BobCompanionApp)` plus
unbound method calls.
"""
import json
import stat

import pytest

from dark_army_daemon import paths
from dark_army_menubar import app as app_module, self_restart


# ── the ledger ────────────────────────────────────────────────────────────────

def test_prune_drops_stale_keeps_boundary_and_bounds_the_future():
    now = 10_000.0
    stamps = [
        now - self_restart.WINDOW_SECONDS - 1,      # too old
        now - self_restart.WINDOW_SECONDS,          # exactly at the boundary
        now - 5,
        now + self_restart.FUTURE_SLACK_SECONDS,    # just inside the slack
        now + self_restart.FUTURE_SLACK_SECONDS + 1,  # a clock jump
    ]
    assert self_restart.prune(stamps, now) == [
        now - self_restart.WINDOW_SECONDS, now - 5,
        now + self_restart.FUTURE_SLACK_SECONDS,
    ]


def test_prune_ignores_junk_members():
    now = 100.0
    assert self_restart.prune([now, "nope", None, True, [1]], now) == [now]


def test_prune_survives_a_number_no_float_can_hold():
    """`_health_check` calls this from a bare timer callback with no `try`
    above it, so one exception here is the recovery never running."""
    now = 100.0
    junk = [10 ** 400, -(10 ** 400), float("inf"), float("-inf"), float("nan")]
    assert self_restart.prune(junk + [now], now) == [now]


def test_a_corrupt_ledger_cannot_wedge_the_allowance_shut():
    """Junk is dropped rather than counted, so it can neither spend the
    allowance nor be mistaken for a stamp."""
    now = 100.0
    assert self_restart.may_auto_restart([10 ** 400] * 5, now) is True


def test_may_auto_restart_counts_up_to_the_cap():
    now = 10_000.0
    assert self_restart.may_auto_restart([], now) is True
    assert self_restart.may_auto_restart([now], now) is True
    assert self_restart.may_auto_restart([now, now], now) is True
    assert self_restart.may_auto_restart([now, now, now], now) is False


def test_the_allowance_returns_once_the_oldest_ages_out():
    now = 10_000.0
    stamps = [now - self_restart.WINDOW_SECONDS - 1, now - 10, now - 5]
    assert self_restart.may_auto_restart(stamps, now) is True


@pytest.mark.parametrize("write", [
    None,
    "not json at all",
    "[1, 2, 3]",
    '{"auto_restarts": "nope", "notice": 7}',
    # A JSON integer too large for a float. Python's ints are unbounded, so it
    # passes an isinstance test and then raises OverflowError on float().
    '{"auto_restarts": [%s], "notice": ""}' % ("9" * 400),
    # json.loads accepts these three, and every one of them is a float that no
    # comparison in `prune` can order.
    '{"auto_restarts": [Infinity, -Infinity, NaN], "notice": ""}',
])
def test_load_never_raises_and_always_returns_a_usable_state(tmp_path, write):
    path = tmp_path / "restart-watch.json"
    if write is not None:
        path.write_text(write, encoding="utf-8")
    state = self_restart.load(path)
    assert isinstance(state["auto_restarts"], list)
    assert isinstance(state["notice"], str)
    # Whatever came back is usable arithmetic, not something that raises on
    # the first comparison.
    assert self_restart.may_auto_restart(state["auto_restarts"], 100.0) is True


def test_load_on_a_directory_is_empty(tmp_path):
    d = tmp_path / "restart-watch.json"
    d.mkdir()
    assert self_restart.load(d) == self_restart._empty()


def test_an_unknown_key_survives_a_round_trip(tmp_path):
    path = tmp_path / "restart-watch.json"
    state = self_restart._empty()
    state["from_a_newer_build"] = {"deep": 1}
    self_restart.save(state, path)
    assert self_restart.load(path)["from_a_newer_build"] == {"deep": 1}


def test_record_auto_restart_appends_one_stamp_and_leaves_a_notice(tmp_path):
    path = tmp_path / "restart-watch.json"
    self_restart.record_auto_restart(1000.0, path)
    state = self_restart.load(path)
    assert state["auto_restarts"] == [1000.0]
    assert state["notice"] == self_restart.NOTICE_RESTARTED


def test_the_recorder_does_not_enforce_the_cap(tmp_path):
    """Counting is `may_auto_restart`'s job; the recorder only records."""
    path = tmp_path / "restart-watch.json"
    for i in range(4):
        self_restart.record_auto_restart(1000.0 + i, path)
    stamps = self_restart.load(path)["auto_restarts"]
    assert stamps == [1000.0, 1001.0, 1002.0, 1003.0]
    assert self_restart.may_auto_restart(stamps, 1003.0) is False


def test_a_notice_that_could_not_be_cleared_is_not_said(tmp_path, monkeypatch):
    """A returned-but-uncleared notice is still on disk, so it would reappear
    on every launch from then on. Say nothing instead."""
    path = tmp_path / "restart-watch.json"
    self_restart.record_auto_restart(1000.0, path)

    def _refuse(state, target=None):
        raise OSError("read-only state directory")

    monkeypatch.setattr(self_restart, "save", _refuse)
    assert self_restart.take_pending_notice(path) == ""


def test_the_give_up_wording_names_the_right_click():
    """A left click opens the panel; only a right click reaches those rows."""
    assert "Right-click the menu bar icon" in self_restart.GAVE_UP_BODY


def test_take_pending_notice_is_read_once(tmp_path):
    path = tmp_path / "restart-watch.json"
    self_restart.record_auto_restart(1000.0, path)
    assert self_restart.take_pending_notice(path) == self_restart.NOTICE_RESTARTED
    assert self_restart.take_pending_notice(path) == ""


def test_an_ordinary_launch_says_nothing_and_writes_nothing(tmp_path):
    path = tmp_path / "restart-watch.json"
    assert self_restart.take_pending_notice(path) == ""
    assert not path.exists()


def test_save_leaves_the_file_private(tmp_path):
    path = tmp_path / "restart-watch.json"
    self_restart.save(self_restart._empty(), path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_marker_is_not_a_private_files_member():
    """Decided in the plan: nothing in it names a path, and it is already
    written 0600 inside a 0700 directory."""
    assert paths.RESTART_WATCH_PATH.name not in paths._PRIVATE_FILES


# ── the app's decisions ───────────────────────────────────────────────────────

class _Thread:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


class _Notifier:
    def __init__(self):
        self.notices = []

    def post_notice(self, ident, title, body):
        self.notices.append((ident, title, body))
        return True


class _Panel:
    available = True


class _Menu:
    """Enough of rumps' menu for the emergency rows.

    `self.menu = [...]` on a rumps.App goes through a property whose setter
    calls `self._menu.update(iterable)`, so the seam is `_menu`, not `menu`.
    """

    def __init__(self):
        self.rows = {}

    def update(self, iterable):
        for item in iterable:
            if item is None:
                continue
            self.rows[item.title] = item

    def keys(self):
        return list(self.rows.keys())

    def __getitem__(self, key):
        return self.rows[key]


def _bare_app(alive=False, **attrs):
    from dark_army_menubar.app import BobCompanionApp
    app = object.__new__(BobCompanionApp)
    if alive is not None:
        app._daemon_thread = _Thread(alive)
    app._restarting = False
    app._health_seen_tick = False
    app._restart_gave_up = False
    app._auto_restart_pending = False
    app._notifier = _Notifier()
    app._panel = _Panel()
    app.restarts = []
    app._on_restart = lambda _: app.restarts.append(1)
    for key, value in attrs.items():
        setattr(app, key, value)
    return app


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    """Point the module's default path at a temp file.

    `paths._home()` already redirects the whole state directory under pytest;
    this narrows it further to one file per test so the cases cannot see each
    other's stamps.
    """
    path = tmp_path / "restart-watch.json"
    monkeypatch.setattr(paths, "RESTART_WATCH_PATH", path)
    return path


def _tick(app):
    from dark_army_menubar.app import BobCompanionApp
    BobCompanionApp._health_check(app, None)


def test_a_live_daemon_restarts_nothing(_ledger):
    app = _bare_app(alive=True)
    _tick(app)
    _tick(app)
    assert app.restarts == []
    assert app._notifier.notices == []
    assert not _ledger.exists()


def test_the_first_tick_only_arms(_ledger):
    app = _bare_app(alive=False)
    _tick(app)
    assert app.restarts == []
    assert app._health_seen_tick is True


def test_the_second_tick_restarts_once_and_stamps_the_ledger(_ledger):
    app = _bare_app(alive=False)
    _tick(app)
    _tick(app)
    assert app.restarts == [1]
    assert app._auto_restart_pending is True
    assert len(self_restart.load(_ledger)["auto_restarts"]) == 1
    assert self_restart.load(_ledger)["notice"] == self_restart.NOTICE_RESTARTED


def test_a_stamp_that_will_not_write_is_a_spent_allowance(monkeypatch, _ledger):
    """The count is the only bound on the flapping. A write that fails
    silently would relaunch every seventy seconds for ever on a state
    directory nobody can write."""
    def _refuse(now, path=None):
        raise OSError("read-only state directory")

    monkeypatch.setattr(self_restart, "record_auto_restart", _refuse)
    app = _bare_app(alive=False)
    app._menu = _Menu()
    _tick(app)
    _tick(app)
    assert app.restarts == []
    assert app._restart_gave_up is True
    assert app._auto_restart_pending is False
    assert app._notifier.notices == [
        (self_restart.GAVE_UP_IDENT, self_restart.GAVE_UP_TITLE,
         self_restart.GAVE_UP_BODY)]
    # And a person can still press Restart: nothing claimed `_restarting`,
    # and no menu row was greyed on the way past.
    assert app._restarting is False
    assert [k for k in app.menu.keys() if k] == [
        app_module.KILL_SWITCH_TITLE,
        "Open Log", "Restart Dark Army", "Quit Dark Army"]


def test_a_teardown_already_in_flight_is_left_alone(_ledger):
    app = _bare_app(alive=False, _restarting=True)
    _tick(app)
    _tick(app)
    assert app.restarts == []


def test_no_daemon_thread_yet_never_restarts(_ledger):
    """The window between __init__ and _start_daemon_thread is not a death."""
    app = _bare_app(alive=None)
    for _ in range(4):
        _tick(app)
    assert app.restarts == []


def test_a_spent_allowance_gives_up_and_says_so_once(_ledger):
    import time as _time
    now = _time.time()
    _ledger.write_text(json.dumps(
        {"schema": 1, "auto_restarts": [now - 3, now - 2, now - 1],
         "notice": ""}), encoding="utf-8")
    app = _bare_app(alive=False)
    app._menu = _Menu()
    _tick(app)
    _tick(app)
    assert app.restarts == []
    assert app._restart_gave_up is True
    assert app._notifier.notices == [
        (self_restart.GAVE_UP_IDENT, self_restart.GAVE_UP_TITLE,
         self_restart.GAVE_UP_BODY)]
    assert [k for k in app.menu.keys() if k] == [
        app_module.KILL_SWITCH_TITLE,
        "Open Log", "Restart Dark Army", "Quit Dark Army"]
    _tick(app)
    assert len(app._notifier.notices) == 1
    assert app.restarts == []


def test_the_teardown_itself_stamps_nothing(monkeypatch, _ledger):
    """`_health_check` owns the stamp, on the main thread and before the
    press. `_restart_now` writes no ledger on either route, so a person's
    press cannot be counted against the cap however it is reached."""
    _drive_restart_now(monkeypatch, auto=False)
    assert not _ledger.exists()
    _drive_restart_now(monkeypatch, auto=True)
    assert not _ledger.exists()


def _drive_restart_now(monkeypatch, auto):
    from dark_army_menubar import app as app_mod
    from dark_army_menubar.app import BobCompanionApp

    class _Quittable:
        def quit(self):
            pass

    app = object.__new__(BobCompanionApp)
    app._auto_restart_pending = auto
    app._panel = _Quittable()
    app._release_dictation_if_down = lambda _reason: None
    app._shutdown_daemon = lambda: None
    monkeypatch.setattr(app_mod.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(app_mod.logging, "shutdown", lambda: None)
    monkeypatch.setattr(app_mod, "_relaunch_command", lambda: "true")

    class _Exit(Exception):
        pass

    def _fake_exit(code):
        raise _Exit()

    monkeypatch.setattr(app_mod.os, "_exit", _fake_exit)
    with pytest.raises(_Exit):
        BobCompanionApp._restart_now(app)


def test_the_emergency_menu_is_built_when_the_cap_is_spent_and_only_once():
    from dark_army_menubar.app import BobCompanionApp
    app = _bare_app(alive=False, _restart_gave_up=True)
    app._menu = _Menu()
    BobCompanionApp._install_emergency_menu_if_needed(app)
    first = list(app.menu.keys())
    assert [k for k in first if k] == [
        app_module.KILL_SWITCH_TITLE,
        "Open Log", "Restart Dark Army", "Quit Dark Army"]
    rows = [app.menu[k] for k in first if k]
    BobCompanionApp._install_emergency_menu_if_needed(app)
    assert [app.menu[k] for k in app.menu.keys() if k] == rows


def test_a_healthy_panel_still_gets_no_menu():
    from dark_army_menubar.app import BobCompanionApp
    app = _bare_app(alive=True)
    app._menu = _Menu()
    BobCompanionApp._install_emergency_menu_if_needed(app)
    assert list(app.menu.keys()) == []


def test_an_id_less_banner_tap_reaches_nothing():
    """The notice carries no session, so `_handle_response` returns before
    `_on_action` — inert by construction, not by a new guard."""
    from dark_army_menubar import notifier as notifier_mod
    calls = []
    n = object.__new__(notifier_mod.Notifier)
    n._on_action = lambda action, sid: calls.append((action, sid))
    notifier_mod.Notifier._handle_response(n, notifier_mod.ACTION_OPEN, "")
    assert calls == []
    notifier_mod.Notifier._handle_response(n, notifier_mod.ACTION_OPEN, "s1")
    assert calls == [(notifier_mod.ACTION_OPEN, "s1")]


def test_post_notice_is_silent_without_a_centre():
    """Every failure ends in silence and a log line, `post`'s own rule."""
    from dark_army_menubar import notifier as notifier_mod
    n = object.__new__(notifier_mod.Notifier)
    n._ns = None
    n._center = None
    n._authorized = None
    assert notifier_mod.Notifier.post_notice(n, "id", "t", "b") is False


def test_the_notice_waits_only_while_an_answer_is_still_coming():
    """rumps fires a Timer on the pass it is started — the interval is the
    repeat, not a delay — so the wait is this predicate's job, and the notice
    is not taken out of the ledger until there is going to be a post."""
    from dark_army_menubar.app import announce_should_wait, ANNOUNCE_WAIT_PASSES
    # The authorization prompt is still up.
    assert announce_should_wait(True, None, 0) is True
    assert announce_should_wait(True, None, ANNOUNCE_WAIT_PASSES - 1) is True
    # Bounded: an unanswered prompt is posted into eventually, not held for ever.
    assert announce_should_wait(True, None, ANNOUNCE_WAIT_PASSES) is False
    # Answered, either way — nothing left to wait for.
    assert announce_should_wait(True, True, 0) is False
    assert announce_should_wait(True, False, 0) is False
    # No bundle to post from: a banner can never be drawn, so do not hold one.
    assert announce_should_wait(False, None, 0) is False


def test_the_marker_is_not_a_preference():
    from dark_army_menubar.preferences import DEFAULTS
    assert not [k for k in DEFAULTS if "restart_watch" in k]
