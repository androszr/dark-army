"""The panel-visibility verb's alert-suppression contract.

The heartbeat's only sender is gone; the verb is kept for the EOF clear and
an older panel. The flag that carries this has two remaining safeties (the
daemon's TTL and the EOF clear in panel_process) and each is pinned here.

All pure Python: the daemon's two scalars and a clock, the policy with dicts,
the panel process with a fake Popen. No AppKit.
"""

import time

import pytest

import dark_army_daemon.daemon as daemon_mod
from dark_army_daemon.alerts import AlertPolicy
from dark_army_daemon.daemon import BobDaemon, PANEL_VISIBLE_TRUST_SECONDS
from dark_army_menubar.panel_process import PanelProcess


SNAPSHOT = {
    "waiting": [{"session_id": "s1", "nickname": "Gil", "project": "p",
                 "branch": "", "signals": []}],
    # A background-agent row with no _session_states entry — the reason
    # _alert_suppressed reads the snapshot rather than the state map.
    "running": [{"session_id": "bg-1", "nickname": "Mira", "project": "p",
                 "kind": "background", "signals": []}],
    "sleeping": [],
    "finished": [{"session_id": "f-done", "signals": []}],
}


# ── the daemon flag ──────────────────────────────────────────────────────────

def test_panel_visible_suppresses_every_snapshot_session():
    d = BobDaemon()
    d.note_panel_visible(True)
    out = d._alert_suppressed(SNAPSHOT)
    # Every session id found in the snapshot's entries, background rows
    # included (and `finished` too — harmless, since evaluate skips it).
    assert {"s1", "bg-1", "f-done"} <= out


def test_stale_panel_reading_contributes_nothing():
    """The TTL fails open: a dead or hung panel costs at most 15s of silence."""
    d = BobDaemon()
    d.note_panel_visible(True)
    d._panel_visible_at = time.time() - (PANEL_VISIBLE_TRUST_SECONDS + 1.0)
    assert d._alert_suppressed(SNAPSHOT) == set()


def test_future_timestamp_is_distrusted():
    """Clock weirdness fails open too — never trust a future stamp."""
    d = BobDaemon()
    d.note_panel_visible(True)
    d._panel_visible_at = time.time() + 3600.0
    assert d._alert_suppressed(SNAPSHOT) == set()


def test_panel_hidden_contributes_nothing_immediately():
    d = BobDaemon()
    d.note_panel_visible(True)
    d.note_panel_visible(False)
    assert d._alert_suppressed(SNAPSHOT) == set()


def test_panel_union_keeps_the_frontmost_sessions():
    """The two suppression sources are a union — neither drops the other."""
    d = BobDaemon()
    d._session_states["front"] = {"pid": 4242}
    d._frontmost_pids = {4242}
    d._frontmost_at = time.time()
    # Frontmost alone works with the panel hidden…
    assert d._alert_suppressed(SNAPSHOT) == {"front"}
    # …and survives the panel turning up.
    d.note_panel_visible(True)
    out = d._alert_suppressed(SNAPSHOT)
    assert "front" in out
    assert {"s1", "bg-1"} <= out


def test_no_snapshot_means_no_panel_suppression():
    """The panel term reads the snapshot it was handed, nothing else."""
    d = BobDaemon()
    d.note_panel_visible(True)
    assert d._alert_suppressed() == set()


# ── the policy defers, never loses ───────────────────────────────────────────

def _waiting_snapshot():
    return {"waiting": [{"session_id": "s1", "nickname": "Gil", "project": "p",
                         "branch": "", "signals": []}]}


def test_suppressed_card_alert_is_deferred_not_lost():
    policy = AlertPolicy()
    cards = {"s1": {"message": "Waiting for input", "hook": "Stop"}}
    now = time.time()
    assert policy.evaluate(_waiting_snapshot(), cards, now,
                           suppressed={"s1"}) == []
    # The next tick without suppression fires: the dedupe stamp and the
    # cooldown were not falsely burned by the suppressed tick.
    raised = policy.evaluate(_waiting_snapshot(), cards, now + 1.0,
                             suppressed=())
    assert len(raised) == 1
    assert raised[0].rule == "card"
    assert raised[0].session_id == "s1"


def test_suppressed_permission_prompt_is_deferred_not_lost():
    """The `_fired` key is a request id and request ids never recur — stamping
    it on a suppressed tick would convert deferral into permanent loss."""
    policy = AlertPolicy()
    snapshot = {"running": [{"session_id": "s2", "nickname": "Din",
                             "project": "p", "branch": "", "signals": []}]}
    prompts = {"s2": {"request_id": "req-1", "tool_name": "Bash",
                      "input_preview": "rm -rf build"}}
    now = time.time()
    assert policy.evaluate(snapshot, {}, now, suppressed={"s2"},
                           prompts=prompts) == []
    raised = policy.evaluate(snapshot, {}, now + 1.0, suppressed=(),
                             prompts=prompts)
    assert len(raised) == 1
    assert raised[0].rule == "permission"
    # And once fired, the same request never fires again.
    assert policy.evaluate(snapshot, {}, now + 2.0, suppressed=(),
                           prompts=prompts) == []


# ── the EOF clear ────────────────────────────────────────────────────────────

def test_read_events_eof_clears_the_flag_exactly_once():
    """Panel death closes the pipe; the reader's last act is the clear."""
    calls = []
    proc = type("FakeProc", (), {"stdout": iter([])})()
    p = PanelProcess(on_action=lambda name, value: calls.append((name, value)))
    p._read_events(proc)
    assert calls == [("panel_visibility", False), ("dictate_up", None)]


def test_read_events_eof_releases_a_held_dictation_key():
    """A panel that dies mid-hold must release the key on the same EOF."""
    calls = []
    proc = type("FakeProc", (), {"stdout": iter([])})()
    p = PanelProcess(on_action=lambda name, value: calls.append((name, value)))
    p._read_events(proc)
    assert ("dictate_up", None) in calls


def test_read_events_clears_after_real_traffic_too():
    calls = []
    lines = [b'{"event": "action", "name": "open_log"}\n', b"not json\n"]
    proc = type("FakeProc", (), {"stdout": iter(lines)})()
    p = PanelProcess(on_action=lambda name, value: calls.append((name, value)))
    p._read_events(proc)
    assert calls == [("open_log", None), ("panel_visibility", False),
                     ("dictate_up", None)]


def test_read_events_survives_a_failing_clear():
    """The clear is guarded: a handler that raises must not raise out of the
    reader thread."""
    def explode(name, value):
        raise RuntimeError("boom")
    proc = type("FakeProc", (), {"stdout": iter([])})()
    p = PanelProcess(on_action=explode)
    p._read_events(proc)   # must not raise


# ── preferences and routing ──────────────────────────────────────────────────

def test_every_persisted_preference_is_read_back_into_settings():
    """A preference the menu bar saves but never loads is write-only.

    `_push_panel_context` ships `{**self._settings}` and the panel owns the
    settings UI, so a key absent from that dict is a key the panel never sees:
    the toggle works until the process ends, the value is written to disk, and
    then the panel's own default wins on every launch afterwards.

    Asserted against DEFAULTS rather than against a list of names, so the next
    preference someone adds is covered the day it is added. Read out of the
    source: constructing the app needs rumps, a status item and a daemon.
    """
    import ast
    from pathlib import Path

    from dark_army_menubar import app as app_mod
    from dark_army_menubar.preferences import DEFAULTS

    src = Path(app_mod.__file__).read_text()
    keys: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        # The one `self._settings: dict = {...}` literal in __init__.
        if (isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Attribute)
                and node.target.attr == "_settings"
                and isinstance(node.value, ast.Dict)):
            keys = {k.value for k in node.value.keys
                    if isinstance(k, ast.Constant)}
            break

    assert keys, "could not find the _settings literal in app.py"
    missing = sorted(set(DEFAULTS) - keys)
    assert not missing, f"saved but never loaded: {missing}"


def test_removed_panel_actions_are_ignored_not_dispatched(caplog):
    """An older panel binary still sends the fifteen removed verbs;
    `_on_panel_action` logs and returns rather than dispatching."""
    import logging
    from dark_army_menubar.app import BobCompanionApp

    removed = (
        "set_session_timeout",
        "set_terminal_title",
        "set_tldr_hint",
        "set_work_report",
        "set_permission_broker",
        "set_session_title",
        "set_board_wrap_up",
        "set_card_prepare",
        "set_board_filing",
        "set_statusline",
        "set_launch_at_login",
        "set_panel_docked",
        "set_dock_summon",
        "set_dock_ribbon_style",
        "set_dock_locked",
    )
    for name in removed:
        assert name not in BobCompanionApp.PANEL_ACTIONS, name

    fake = type("FakeApp", (), {})()
    fake._daemon = None
    fake.PANEL_ACTIONS = BobCompanionApp.PANEL_ACTIONS
    with caplog.at_level(logging.WARNING, logger="dark-army.menubar"):
        for name in removed:
            BobCompanionApp._on_panel_action(fake, name, True)
    for name in removed:
        assert f"Ignoring unknown panel action {name!r}" in caplog.text
