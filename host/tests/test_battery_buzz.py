"""The phone buzzes once when the Mac's battery runs low.

The rule (`alerts.battery_buzz`), the row (`alerts.battery_alert`), the
daemon's decision and delivery, the setter, the menu-bar plumbing and the
source pins. The daemon cases borrow `test_alert_delivery.py`'s seams."""
import inspect
import re
from pathlib import Path

import pytest

from dark_army_daemon import alerts, buzz_ledger, relay_client
from dark_army_daemon import daemon as daemon_module
from dark_army_daemon.daemon import BobDaemon

from tests.test_alert_delivery import (_Connector, _banner_seen, _idle,
                                       _outcomes, _pushable, _settle)

REPO = Path(__file__).resolve().parents[2]


def _r(percent, source="battery", **extra):
    reading = {"available": True, "present": True, "percent": percent,
               "source": source, "charge": ""}
    reading.update(extra)
    return reading


# -- the rule -----------------------------------------------------------------

def test_off_never_fires_and_rearms():
    assert alerts.battery_buzz(_r(5), 0, False, None, 60.0) == (False, True, None)
    assert alerts.battery_buzz(_r(5), 0, True, None, 60.0) == (False, True, None)


def test_ac_rearms_even_at_low_percent():
    assert alerts.battery_buzz(_r(5, "ac"), 20, False, None, 60.0) == (False, True, None)


def test_above_the_level_does_nothing():
    assert alerts.battery_buzz(_r(21), 20, True, None, 60.0) == (False, True, None)
    assert alerts.battery_buzz(_r(21), 20, False, None, 60.0) == (False, False, None)


def test_at_the_level_fires_once():
    assert alerts.battery_buzz(_r(20), 20, True, None, 60.0) == (True, False, 20)


def test_the_same_reading_again_is_silent():
    assert alerts.battery_buzz(_r(20), 20, False, None, 60.0) == (False, False, None)


def test_lower_while_disarmed_is_silent():
    assert alerts.battery_buzz(_r(19), 20, False, None, 60.0) == (False, False, None)


def test_a_discharge_after_ac_fires_again():
    armed, at = True, None
    fires = []
    for reading in (_r(20), _r(19), _r(60, "ac"), _r(20)):
        fire, armed, at = alerts.battery_buzz(reading, 20, armed, None, 60.0, at)
        fires.append(fire)
    assert fires == [True, False, False, True]


def test_hovering_at_the_line_costs_one_buzz():
    armed, at = True, None
    fires = []
    for reading in (_r(20), _r(21), _r(20)):
        fire, armed, at = alerts.battery_buzz(reading, 20, armed, None, 60.0, at)
        fires.append(fire)
    assert fires == [True, False, False]


def test_the_success_criterion_sequence():
    armed, at, out = True, None, []
    for p, s in [(21, "battery"), (20, "battery"), (19, "battery"),
                 (21, "battery"), (20, "battery"), (50, "ac"),
                 (20, "battery")]:
        fire, armed, at = alerts.battery_buzz(_r(p, s), 20, armed, None, 60.0, at)
        out.append(fire)
    assert out == [False, True, False, False, False, False, True]


def _run(readings, level=20):
    armed, at, fires = True, None, []
    for reading in readings:
        fire, armed, at = alerts.battery_buzz(reading, level, armed, None,
                                              60.0, at)
        fires.append(fire)
    return fires


def test_an_unseen_overnight_charge_rearms():
    assert _run([_r(20), _r(100), _r(60), _r(20), _r(10)]) == [
        True, False, False, True, False]


def test_a_rise_below_the_margin_stays_silent():
    assert _run([_r(20), _r(24), _r(20)]) == [True, False, False]
    assert _run([_r(20), _r(25), _r(20)]) == [True, False, True]


def test_a_person_at_the_mac_keeps_it_armed():
    assert alerts.battery_buzz(_r(20), 20, True, 10.0, 60.0) == (False, True, None)
    assert alerts.battery_buzz(_r(20), 20, True, 61.0, 60.0) == (True, False, 20)


@pytest.mark.parametrize("reading", [
    {"available": False}, {"available": True, "present": False},
    _r(None), "nope", None, _r("20"), _r(True)])
@pytest.mark.parametrize("armed", [True, False])
def test_an_unreadable_reading_changes_nothing(reading, armed):
    assert alerts.battery_buzz(reading, 20, armed, None, 60.0) == (False, armed, None)


# -- the row ------------------------------------------------------------------

def test_the_row_is_a_machine_row():
    row = alerts.battery_alert(20, 1000.5)
    assert row["session_id"] == "" and row["kind"] == "battery"
    assert row["rule"] == "battery" and row["actions"] == []
    assert row["answerable"] is False
    assert row["need"] == "20% left on battery — plug the Mac in"
    assert row["id"] == "battery:1000"
    assert set(row) >= {"id", "kind", "need", "rule", "title"}


def test_the_kinds_are_pinned_in_order():
    assert alerts.KINDS[:2] == ("security", "battery")
    assert alerts.KINDS == relay_client.PUSH_KINDS
    assert alerts.MACHINE_KINDS == ("security", "battery")
    assert "battery" not in relay_client.ACTIVITY_KINDS
    assert "battery" not in alerts.FACE_KINDS


def test_the_composed_fields_name_nothing_but_the_machine():
    row = alerts.battery_alert(20, 1.0)
    assert BobDaemon._compose_push_title([row]) == "The Mac's battery is running low"
    assert BobDaemon._compose_push_need([row]) == row["need"]
    assert BobDaemon._compose_push_face([row]) == ""
    assert BobDaemon._compose_push_kind([row, {"kind": "permission"}]) == "battery"
    assert BobDaemon._split_unlisted([row], set()) == ([], [row])


def test_the_title_branch_source_avoids_the_banned_words():
    src = inspect.getsource(BobDaemon._compose_push_title)
    for word in ('"title"', '"subtitle"', '"body"'):
        assert word not in src


# -- the setter ---------------------------------------------------------------

@pytest.mark.parametrize("given,want", [
    ("abc", 0), (150, 100), (-5, 0), (20, 20), ("30", 30), (None, 0)])
def test_the_setter_clamps(given, want):
    d = BobDaemon.__new__(BobDaemon)
    d.set_battery_buzz_percent(given)
    assert d.battery_buzz_percent == want


# -- the daemon ---------------------------------------------------------------

def _bare(monkeypatch, idle=None, percent=20, source="battery", level=20):
    monkeypatch.setattr(buzz_ledger, "mac_idle_seconds", lambda: idle)
    d = BobDaemon.__new__(BobDaemon)
    d.battery_buzz_percent = level
    d._power = _r(percent, source)
    return d


def test_the_decision_fires_once(monkeypatch):
    d = _bare(monkeypatch)
    d._decide_battery_buzz()
    row = d._battery_buzz_pending
    assert row["kind"] == "battery" and "20%" in row["need"]
    assert d._battery_armed is False
    assert d._alerts == [row]
    d._decide_battery_buzz()
    assert d._battery_buzz_pending is row and d._alerts == [row]


def test_the_decision_waits_while_the_person_is_at_the_mac(monkeypatch):
    d = _bare(monkeypatch, idle=5.0)
    d._decide_battery_buzz()
    assert d.__dict__.get("_battery_buzz_pending") is None
    assert d._battery_armed is True
    monkeypatch.setattr(buzz_ledger, "mac_idle_seconds", lambda: 120.0)
    d._decide_battery_buzz()
    assert d._battery_buzz_pending is not None


def test_the_decision_ignores_a_missing_reading_and_off(monkeypatch):
    d = _bare(monkeypatch)
    del d.__dict__["_power"]
    d._decide_battery_buzz()
    assert d.__dict__.get("_battery_buzz_pending") is None
    d = _bare(monkeypatch, level=0)
    d._decide_battery_buzz()
    assert d.__dict__.get("_battery_buzz_pending") is None
    assert d._battery_armed is True


def _pending(d, percent=20):
    d._battery_buzz_pending = alerts.battery_alert(percent, 100.0)


@pytest.mark.asyncio
async def test_delivery_pushes_one_phone_only_buzz(monkeypatch):
    d = _pushable(monkeypatch)
    seen = _banner_seen(d)
    calls = _outcomes(d, monkeypatch)
    _pending(d)
    d._deliver_battery_buzz()
    await _settle(d)
    ((device, body),) = d._relay_connector.pushed
    assert device == "phone-1" and body["kind"] == "battery"
    assert body["title"] == "The Mac's battery is running low"
    assert body["need"] == "20% left on battery — plug the Mac in"
    assert isinstance(body["badge"], int)
    for key in ("session_id", "card_id", "face", "work", "act"):
        assert key not in body
    assert seen == []
    ((ids, outcome, extra),) = calls
    assert outcome == "sent:1" and extra["banner"] is False
    assert extra["held_seconds"] == 0.0
    d._deliver_battery_buzz()
    await _settle(d)
    assert len(d._relay_connector.pushed) == 1


@pytest.mark.asyncio
async def test_delivery_obeys_every_gate(monkeypatch):
    d = _pushable(monkeypatch)
    d.phone_push_enabled = False
    _pending(d)
    d._deliver_battery_buzz()
    await _settle(d)
    d.phone_push_enabled = True
    d.remote_access_enabled = False
    _pending(d)
    d._deliver_battery_buzz()
    await _settle(d)
    d.remote_access_enabled = True
    d._relay_connector = None
    _pending(d)
    d._deliver_battery_buzz()
    await _settle(d)
    d._relay_connector = _Connector()
    _idle(monkeypatch, 10.0)
    calls = _outcomes(d, monkeypatch)
    _pending(d)
    d._deliver_battery_buzz()
    await _settle(d)
    assert d._relay_connector.pushed == []
    assert [o for _, o, _ in calls] == ["withheld:mac_active"]


# -- the menu bar -------------------------------------------------------------

def test_the_menu_bar_plumbing(monkeypatch):
    from dark_army_menubar import app as A
    from dark_army_menubar.preferences import DEFAULTS

    assert DEFAULTS["battery_buzz_percent"] == 0
    assert "set_battery_buzz_percent" in A.BobCompanionApp.PANEL_ACTIONS
    saved, told = [], []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    instance = A.BobCompanionApp.__new__(A.BobCompanionApp)
    instance._settings = {}
    instance._daemon = type("D", (), {
        "set_battery_buzz_percent": lambda self, v: told.append(v)})()
    A.BobCompanionApp.PANEL_ACTIONS["set_battery_buzz_percent"](instance, "20")
    assert instance._settings["battery_buzz_percent"] == 20
    assert saved == [{"battery_buzz_percent": 20}] and told == [20]


def test_load_preferences_without_the_key_answers_zero(tmp_path):
    from dark_army_menubar.preferences import load_preferences

    path = tmp_path / "preferences.json"
    path.write_text("{}")
    assert load_preferences(path=path)["battery_buzz_percent"] == 0


# -- source pins --------------------------------------------------------------

def _src(rel):
    return (REPO / rel).read_text()


def test_swift_and_relay_pins():
    assert _src("panel/Sources/BobPanel/Settings.swift").count(
        'raw["battery_buzz_percent"] as? Int') == 1
    rows = _src("panel/Sources/BobPanel/SettingsDeviceRows.swift")
    assert rows.count('action: "set_battery_buzz_percent"') == 1
    assert rows.count('"submenu:battery-buzz"') == 1
    assert re.search(r'battery:\s*"buzz-permission.wav"',
                     _src("relay/api/push.js"))


def test_daemon_wiring_pins():
    src = inspect.getsource(daemon_module)
    assert src.count("_start_phone_leg([row], banner=False)") == 1
    deliver = inspect.getsource(BobDaemon._deliver_battery_buzz)
    assert "_start_phone_leg([row], banner=False)" in deliver
    assert "_deliver_batch" not in deliver and "on_alerts" not in deliver
    push = inspect.getsource(BobDaemon._push_agents_snapshot)
    assert push.index("self._deliver_alerts()") < push.index(
        "self._deliver_battery_buzz()")
    assert "alerting.MACHINE_KINDS" in inspect.getsource(BobDaemon._split_unlisted)
    assert "_decide_battery_buzz()" in inspect.getsource(
        BobDaemon._enrich_agent_stubs)


def test_a_bad_stored_level_does_not_stop_the_menu_bar():
    from dark_army_menubar import app as A

    for bad, want in (("twenty", 0), ([1], 0), (None, 0), (150, 100),
                      (-3, 0), ("30", 30), (float("inf"), 0)):
        assert A._battery_level(bad) == want
