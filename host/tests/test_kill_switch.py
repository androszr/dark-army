"""The kill switch's identification rules.

The whole safety case of this button is that it may only signal a process
whose own argv proves it is running Dark Army's code, plus the tree below
one. Everything here is table-driven against `kill_switch`'s pure half; the
two impure functions (`snapshot`, `terminate`) are exercised through
injected fakes, never against the machine.
"""

import os
import signal

import pytest

from dark_army_menubar import kill_switch as ks


BUNDLE = "/Applications/Dark Army.app"
STATE = "/Users/someone/.dark-army"


def P(pid, ppid, *argv, started=100.0):
    return ks.Proc(pid=pid, ppid=ppid, argv=tuple(argv), started=started)


def owns(*argv):
    return ks.owns(argv, bundle_root=BUNDLE, state_dir=STATE)


# --- What counts as Dark Army's own code ---------------------------------


@pytest.mark.parametrize("argv, reason", [
    ((BUNDLE + "/Contents/MacOS/Dark Army",), "app bundle"),
    ((BUNDLE + "/Contents/Resources/BobPanel.app/Contents/MacOS/BobPanel",
      "--hidden"), "app bundle"),
    ((BUNDLE + "/Contents/Resources/BobPanel.app/Contents/MacOS/Dark Army",
      "--hidden"), "app bundle"),
    ((BUNDLE + "/Contents/Resources/Dark Army.app/Contents/MacOS/Dark Army",
      "--hidden"), "app bundle"),
    ((BUNDLE + "/Contents/MacOS/python", "-m",
      "dark_army_daemon.pty_broker", "--port", "19873"), "app bundle"),
    (("/usr/bin/python3", "-m", "dark_army_daemon.pty_broker"),
     "module dark_army_daemon.pty_broker"),
    (("/usr/bin/python3", "-m", "dark_army_menubar"),
     "module dark_army_menubar"),
    (("/opt/python", STATE + "/dark-army-channel"),
     "dark-army-channel"),
    # The dual-name window's registrations add `--name=`; the rule reads the
    # helper's own argv element and is not disturbed by a flag after it.
    (("/opt/python", STATE + "/dark-army-channel", "--name=dark-army"),
     "dark-army-channel"),
    (("/opt/python", STATE + "/dark-army-channel", "--name=bob"),
     "dark-army-channel"),
    (("/opt/python", STATE + "/dark-army-channel", "--host=codex",
      "--name=dark-army"), "dark-army-channel"),
    (("/usr/bin/python3", STATE + "/dark-army-notify"),
     "dark-army-notify"),
])
def test_the_rules_that_do_match(argv, reason):
    assert owns(*argv) == reason


@pytest.mark.parametrize("argv", [
    # An assistant in somebody's editor. The whole point of the switch is
    # that these carry on.
    ("/Users/someone/.local/bin/claude",),
    ("/Users/someone/.local/bin/claude", "--dangerously-load-development-"
     "channels", "server:bob"),
    ("/Users/someone/.local/bin/claude", "--dangerously-load-development-"
     "channels", "server:dark-army"),
    ("/usr/local/bin/codex", "exec", "--", "do the thing"),
    # A session whose *prompt* quotes one of our paths. Saying a word is not
    # running our code.
    ("/Users/someone/.local/bin/claude",
     "look at " + STATE + "/dark-army-channel and tell me what it does"),
    ("/bin/grep", "-r", "dark_army_daemon.pty_broker", "."),
    # `-m` and the module must be adjacent, and the module exact.
    ("/usr/bin/python3", "-m", "dark_army_daemonish.thing"),
    ("/usr/bin/python3", "-mdark_army_daemon.pty_broker"),
    ("/usr/bin/python3", "-c", "import dark_army_daemon"),
    # A neighbour of the bundle, not inside it. Component-aware containment.
    ("/Applications/Dark Army.app.backup/Contents/MacOS/x",),
    ("/Applications/Dark Armyista.app/Contents/MacOS/x",),
    # A helper's name somewhere else on disk is somebody else's file.
    ("/usr/bin/python3", "/tmp/dark-army-channel"),
    (),
])
def test_the_things_that_must_never_match(argv):
    assert owns(*argv) == ""


def test_an_unstated_root_turns_its_rule_off_rather_than_matching_everything():
    argv = (BUNDLE + "/Contents/MacOS/Dark Army",)
    assert ks.owns(argv, bundle_root="", state_dir="") == ""
    helper = ("/usr/bin/python3", STATE + "/dark-army-channel")
    assert ks.owns(helper, bundle_root="", state_dir="") == ""


def test_an_executable_the_app_can_name_outright_matches():
    """A checkout has no bundle, and the panel binary sits outside one — but
    the app chose that path at startup and can say so."""
    argv = ("/checkout/panel/.build/release/BobPanel", "--hidden")
    assert ks.owns(argv, bundle_root="", state_dir="") == ""
    assert ks.owns(argv, bundle_root="", state_dir="",
                   executables=["/checkout/panel/.build/release/BobPanel"]) \
        == "panel"


# --- The plan -------------------------------------------------------------


def _fleet():
    """The shape of a real machine: the app, its panel and broker, an agent
    inside a Dark Army terminal, and two editor sessions with our channel
    server as their child."""
    return [
        P(1, 0, "/sbin/launchd"),
        P(100, 1, BUNDLE + "/Contents/MacOS/Dark Army"),
        P(101, 100, BUNDLE + "/Contents/Resources/BobPanel.app/Contents/"
                             "MacOS/BobPanel", "--hidden"),
        P(102, 100, BUNDLE + "/Contents/MacOS/python", "-m",
          "dark_army_daemon.pty_broker", "--sock", STATE + "/pty.sock"),
        P(103, 102, "/Users/someone/.local/bin/claude"),   # hosted terminal
        P(104, 103, "/opt/python", STATE + "/dark-army-channel"),
        P(200, 50, "/Applications/Visual Studio Code.app/Contents/MacOS/Code"),
        P(201, 200, "/Users/someone/.local/bin/claude"),   # editor session
        P(202, 201, "/opt/python", STATE + "/dark-army-channel"),
        P(300, 50, "/usr/bin/vim", "notes.md"),
    ]


def _plan(**kw):
    kw.setdefault("bundle_root", BUNDLE)
    kw.setdefault("state_dir", STATE)
    return ks.plan(_fleet(), **kw)


def test_the_plan_names_our_own_and_the_tree_below_it():
    plan = _plan(own_pid=0)
    assert sorted(p.pid for p in plan.victims) == [100, 101, 102, 103, 104, 202]


def test_an_editor_session_and_the_editor_survive():
    pids = {p.pid for p in _plan(own_pid=0).victims}
    assert 200 not in pids, "VS Code was signalled"
    assert 201 not in pids, "an assistant in an editor terminal was signalled"
    assert 300 not in pids
    assert 1 not in pids


def test_the_hosted_terminal_dies_with_its_broker():
    """It is started in its own session, so killing the broker alone would
    orphan it. Being the broker's child is the proof that it is ours."""
    plan = _plan(own_pid=0)
    assert 103 in {p.pid for p in plan.victims}
    assert plan.reasons[103] == "child of 102"


def test_children_are_signalled_before_their_parents():
    order = [p.pid for p in _plan(own_pid=0).victims]
    for child, parent in [(104, 103), (103, 102), (102, 100), (101, 100)]:
        assert order.index(child) < order.index(parent), (child, parent)


def test_the_pressing_process_is_left_off_the_list_but_its_tree_is_not():
    plan = _plan(own_pid=100)
    pids = [p.pid for p in plan.victims]
    assert 100 not in pids
    assert 100 not in plan.reasons
    assert {101, 102, 103, 104} <= set(pids)


def test_an_over_wide_sweep_signals_nothing_at_all():
    """The one failure this button must never have. A rule gone wrong is a
    refusal with the evidence, not a thousand signals."""
    procs = [P(100, 1, BUNDLE + "/Contents/MacOS/Dark Army")]
    procs += [P(1000 + i, 100 + i, "/bin/child")
              for i in range(ks.MAX_VICTIMS + 5)]
    # Chain them so each is the previous one's child.
    procs = [procs[0]] + [P(1000 + i, 100 if i == 0 else 999 + i, "/bin/child")
                          for i in range(ks.MAX_VICTIMS + 5)]
    plan = ks.plan(procs, bundle_root=BUNDLE, state_dir=STATE, own_pid=0)
    assert plan.overflowed is True
    assert plan.victims == []


def test_nothing_of_ours_running_is_an_empty_plan_not_an_error():
    procs = [P(200, 50, "/usr/bin/vim"), P(201, 200, "/bin/zsh")]
    plan = ks.plan(procs, bundle_root=BUNDLE, state_dir=STATE, own_pid=0)
    assert plan.victims == []
    assert plan.overflowed is False
    assert not plan


# --- terminate ------------------------------------------------------------


class _Killer:
    def __init__(self, gone=()):
        self.calls = []
        self.gone = set(gone)

    def __call__(self, pid, sig):
        if pid in self.gone:
            raise ProcessLookupError(pid)
        self.calls.append((pid, sig))


def test_terminate_asks_first_then_insists():
    kill = _Killer()
    slept = []
    sent = ks.terminate([P(10, 1, "x"), P(11, 1, "y")],
                        kill=kill, sleep=slept.append,
                        still_alive=lambda proc: True)
    assert sent == [10, 11]
    assert kill.calls == [(10, signal.SIGTERM), (11, signal.SIGTERM),
                          (10, signal.SIGKILL), (11, signal.SIGKILL)]
    assert slept == [ks.GRACE_SECONDS]


def test_a_process_that_went_away_is_never_signalled_twice():
    """Pid reuse in the grace window is the risk; start time is the identity
    that answers it."""
    kill = _Killer()
    ks.terminate([P(10, 1, "x"), P(11, 1, "y")],
                 kill=kill, sleep=lambda _s: None,
                 still_alive=lambda proc: proc.pid != 10)
    assert (10, signal.SIGKILL) not in kill.calls
    assert (11, signal.SIGKILL) in kill.calls


def test_a_pid_already_gone_at_sigterm_drops_out_entirely():
    kill = _Killer(gone={10})
    sent = ks.terminate([P(10, 1, "x"), P(11, 1, "y")],
                        kill=kill, sleep=lambda _s: None,
                        still_alive=lambda proc: True)
    assert sent == [11]
    assert all(pid != 10 for pid, _sig in kill.calls)


def test_terminate_never_signals_a_process_group():
    """A negative pid is `kill(2)`'s process-group form, and this module has
    not proved any group's membership."""
    kill = _Killer()
    ks.terminate([P(10, 1, "x")], kill=kill, sleep=lambda _s: None,
                 still_alive=lambda proc: True)
    assert all(pid > 0 for pid, _sig in kill.calls)


def test_an_empty_list_sleeps_for_nothing():
    slept = []
    assert ks.terminate([], kill=_Killer(), sleep=slept.append) == []
    assert slept == []


# --- The app's wiring -----------------------------------------------------


class _MenuItem:
    def __init__(self, title, callback=None):
        self.title = title
        self.callback = callback


class _Menu:
    """Enough of rumps' menu for the emergency rows. rumps keys an item by
    the title it was *built* with, so a retitle must not move the key."""

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


def _app(monkeypatch, **attrs):
    from dark_army_menubar import app as app_module

    monkeypatch.setattr(app_module.rumps, "MenuItem", _MenuItem, raising=False)
    app = object.__new__(app_module.BobCompanionApp)
    app._restarting = False
    app._kill_armed = False
    app._menu = _Menu()
    app.killed = []
    app._kill_now = lambda: app.killed.append(1)
    for key, value in attrs.items():
        setattr(app, key, value)
    return app


def test_the_panel_can_ask_for_it_and_the_verb_is_on_the_table():
    from dark_army_menubar.app import BobCompanionApp

    assert "kill_all" in BobCompanionApp.PANEL_ACTIONS


def test_the_menu_row_arms_before_it_fires(monkeypatch):
    from dark_army_menubar import app as app_module

    app = _app(monkeypatch)
    app._on_kill_switch = lambda sender: \
        app_module.BobCompanionApp._on_kill_switch(app, sender)
    app._retitle_kill_row = lambda title: \
        app_module.BobCompanionApp._retitle_kill_row(app, title)
    app.menu = [_MenuItem(app_module.KILL_SWITCH_TITLE)]
    row = app._menu.rows[app_module.KILL_SWITCH_TITLE]

    started = []
    monkeypatch.setattr(app_module.threading, "Timer",
                        lambda *a, **k: type("T", (), {
                            "daemon": True, "start": lambda self: None})())
    monkeypatch.setattr(app_module.threading, "Thread",
                        lambda *a, **k: type("T", (), {
                            "daemon": True,
                            "start": lambda self: started.append(1)})())

    app._on_kill_switch(row)                 # first click: armed only
    assert app.killed == [] and started == []
    assert app._kill_armed is True
    assert row.title == app_module.KILL_SWITCH_ARMED_TITLE
    # The key did not move with the title, or the second click cannot find it.
    assert app_module.KILL_SWITCH_TITLE in app._menu.keys()

    app._on_kill_switch(row)                 # second click: it goes
    assert started == [1]
    assert app._restarting is True
    assert app._kill_armed is False


def test_the_row_disarms_itself_again(monkeypatch):
    from dark_army_menubar import app as app_module

    app = _app(monkeypatch, _kill_armed=True)
    app.menu = [_MenuItem(app_module.KILL_SWITCH_TITLE)]
    row = app._menu.rows[app_module.KILL_SWITCH_TITLE]
    row.title = app_module.KILL_SWITCH_ARMED_TITLE

    app_module.BobCompanionApp._disarm_kill_switch(app)
    assert app._kill_armed is False
    assert row.title == app_module.KILL_SWITCH_TITLE


def test_the_panels_press_needs_no_second_click(monkeypatch):
    """The panel arms on its own surface, so `kill_all` fires straight away."""
    from dark_army_menubar import app as app_module

    app = _app(monkeypatch)
    started = []
    monkeypatch.setattr(app_module.threading, "Thread",
                        lambda *a, **k: type("T", (), {
                            "daemon": True,
                            "start": lambda self: started.append(1)})())
    app_module.BobCompanionApp._on_kill_switch(app, None)
    assert started == [1]
    assert app._restarting is True


def test_a_teardown_already_under_way_is_never_joined(monkeypatch):
    from dark_army_menubar import app as app_module

    app = _app(monkeypatch, _restarting=True)
    started = []
    monkeypatch.setattr(app_module.threading, "Thread",
                        lambda *a, **k: type("T", (), {
                            "daemon": True,
                            "start": lambda self: started.append(1)})())
    app_module.BobCompanionApp._on_kill_switch(app, None)
    assert started == []


def test_the_emergency_menu_leads_with_the_kill_switch(monkeypatch):
    from dark_army_menubar import app as app_module

    app = _app(monkeypatch, _restart_gave_up=True)
    app._panel = type("P", (), {"available": False})()
    app_module.BobCompanionApp._install_emergency_menu_if_needed(app)
    assert app._menu.keys()[0] == app_module.KILL_SWITCH_TITLE


def _kill_now_app(monkeypatch, plan):
    from dark_army_menubar import app as app_module

    app = _app(monkeypatch)
    app._release_dictation_if_down = lambda _why: None
    app._kill_plan = lambda: plan
    app._disarm_kill_switch = lambda: None
    monkeypatch.setattr(app_module, "callAfter", lambda fn: fn())
    exits, signalled = [], []
    monkeypatch.setattr(app_module.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(app_module.kill_switch, "terminate",
                        lambda victims, **kw: signalled.extend(
                            p.pid for p in victims))
    return app_module, app, exits, signalled


def test_the_press_signals_the_plan_and_then_ends_this_process(monkeypatch):
    plan = ks.Plan(victims=[P(11, 1, "x"), P(12, 1, "y")],
                   reasons={11: "app bundle", 12: "child of 11"})
    app_module, app, exits, signalled = _kill_now_app(monkeypatch, plan)
    app_module.BobCompanionApp._kill_now(app)
    assert signalled == [11, 12]
    assert exits == [0]


def test_an_over_wide_plan_signals_nothing_and_leaves_the_app_running(monkeypatch):
    plan = ks.Plan(victims=[], overflowed=True)
    app_module, app, exits, signalled = _kill_now_app(monkeypatch, plan)
    app_module.BobCompanionApp._kill_now(app)
    assert signalled == []
    assert exits == [], "the app ended itself on a refused sweep"
    assert app._restarting is False, "a refused sweep must be pressable again"


def test_a_sweep_that_blows_up_still_ends_this_process(monkeypatch):
    """Whatever else fails, the press has to end Dark Army — that is the one
    thing the person asked for."""
    app_module, app, exits, _signalled = _kill_now_app(monkeypatch, ks.Plan())

    def _boom():
        raise RuntimeError("no psutil today")

    app._kill_plan = _boom
    app_module.BobCompanionApp._kill_now(app)
    assert exits == [0]


def test_a_checkout_names_no_bundle_root(monkeypatch):
    """Outside a bundle, `dev_build.bundle_path()` falls back to
    `NSBundle.mainBundle()` and answers the Python framework's own
    `Python.app`. Believed, that would put every process running under that
    interpreter on the list — most of a development machine."""
    from dark_army_menubar import app as app_module

    framework = ("/opt/homebrew/Cellar/python@3.12/3.12.13/Frameworks/"
                 "Python.framework/Versions/3.12/Resources/Python.app")
    monkeypatch.setattr(app_module.dev_build, "is_frozen", lambda: False)
    monkeypatch.setattr(app_module.dev_build, "bundle_path",
                        lambda: __import__("pathlib").Path(framework))
    seen = {}
    monkeypatch.setattr(app_module.kill_switch, "snapshot", lambda: [])
    monkeypatch.setattr(app_module.kill_switch, "plan",
                        lambda procs, **kw: seen.update(kw) or ks.Plan())

    app = _app(monkeypatch)
    app._panel = type("P", (), {"executable_path": "/checkout/BobPanel"})()
    app_module.BobCompanionApp._kill_plan(app)
    assert seen["bundle_root"] == ""
    # The panel is still named — outright, by the path the app chose.
    assert seen["executables"] == ["/checkout/BobPanel"]

    # And inside a real bundle it is used.
    monkeypatch.setattr(app_module.dev_build, "is_frozen", lambda: True)
    monkeypatch.setattr(app_module.dev_build, "bundle_path",
                        lambda: __import__("pathlib").Path(BUNDLE))
    app_module.BobCompanionApp._kill_plan(app)
    assert seen["bundle_root"] == BUNDLE


def test_a_refused_sweep_puts_the_row_back(monkeypatch):
    """The press spent the arming, so the row must be retitled outright —
    `_disarm_kill_switch` would find nothing armed and leave it reading
    'click again' for ever."""
    from dark_army_menubar import app as app_module

    app = _app(monkeypatch)
    app.menu = [_MenuItem(app_module.KILL_SWITCH_TITLE)]
    row = app._menu.rows[app_module.KILL_SWITCH_TITLE]
    row.title = app_module.KILL_SWITCH_ARMED_TITLE
    app._release_dictation_if_down = lambda _why: None
    app._kill_plan = lambda: ks.Plan(victims=[], overflowed=True)
    app._retitle_kill_row = lambda title: \
        app_module.BobCompanionApp._retitle_kill_row(app, title)
    monkeypatch.setattr(app_module, "callAfter", lambda fn: fn())
    monkeypatch.setattr(app_module.os, "_exit",
                        lambda code: pytest.fail("ended the process"))

    app_module.BobCompanionApp._kill_now(app)
    assert row.title == app_module.KILL_SWITCH_TITLE
