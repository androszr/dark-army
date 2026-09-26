# host/tests/test_power_source.py
"""The host Mac's battery: `pmset -g batt` parsed, read and memoised, then
published as the omittable `power` section the phone's Fleet tab draws.

The fixture strings are the contract: every shape macOS prints on the
battery line, a desktop's source line alone, and garbage. No test here
depends on the machine it runs on — the reader and the memo are stubbed.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dark_army_daemon import api_server as api_mod
from dark_army_daemon import power_source
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.daemon import BobDaemon, POWER_SOURCE_INTERVAL

THIS_MAC = ("Now drawing from 'AC Power'\n"
            " -InternalBattery-0 (id=22610019)\t73%; charging; "
            "(no estimate) present: true\n")
ON_BATTERY = ("Now drawing from 'Battery Power'\n"
              " -InternalBattery-0 (id=22610019)\t41%; discharging; "
              "3:12 remaining present: true\n")
CHARGED = ("Now drawing from 'AC Power'\n"
           " -InternalBattery-0 (id=22610019)\t100%; charged; "
           "0:00 remaining present: true\n")
NOT_CHARGING = ("Now drawing from 'AC Power'\n"
                " -InternalBattery-0 (id=22610019)\t80%; AC attached; "
                "not charging present: true\n")
FINISHING = ("Now drawing from 'AC Power'\n"
             " -InternalBattery-0 (id=22610019)\t99%; finishing charge; "
             "0:05 remaining present: true\n")
DESKTOP = "Now drawing from 'AC Power'\n"

KEYS = ["available", "charge", "percent", "present", "source"]


# --- the parser ---------------------------------------------------------------


def test_this_mac_charging_on_the_adapter():
    assert power_source.parse(THIS_MAC) == {
        "available": True, "present": True, "percent": 73,
        "source": "ac", "charge": "charging"}


def test_on_battery_discharging_drops_the_time_estimate():
    got = power_source.parse(ON_BATTERY)
    assert got == {"available": True, "present": True, "percent": 41,
                   "source": "battery", "charge": "discharging"}
    assert sorted(got) == KEYS
    assert "3:12" not in repr(got)


def test_charged():
    got = power_source.parse(CHARGED)
    assert (got["percent"], got["source"], got["charge"]) == (100, "ac", "charged")


def test_ac_attached_not_charging():
    got = power_source.parse(NOT_CHARGING)
    assert (got["percent"], got["source"], got["charge"]) == (
        80, "ac", "not_charging")


def test_finishing_charge():
    got = power_source.parse(FINISHING)
    assert (got["percent"], got["source"], got["charge"]) == (
        99, "ac", "finishing")


def test_a_desktop_has_a_source_and_no_battery():
    assert power_source.parse(DESKTOP) == {
        "available": True, "present": False, "percent": None,
        "source": "ac", "charge": ""}


@pytest.mark.parametrize("garbage", ["", "lorem", "Now drawing from 'UPS Power'"])
def test_garbage_is_no_battery_and_no_source(garbage):
    got = power_source.parse(garbage)
    assert got["available"] is True
    assert got["present"] is False
    assert got["percent"] is None
    assert got["source"] == ""
    assert got["charge"] == ""


@pytest.mark.parametrize("text", [THIS_MAC, ON_BATTERY, CHARGED, NOT_CHARGING,
                                  FINISHING, DESKTOP, "", "lorem"])
def test_every_answer_has_the_same_five_keys_and_no_clock(text):
    got = power_source.parse(text)
    assert sorted(got) == KEYS
    moving = api_mod._CLOCK_FIELDS | api_mod._POLL_ECHO_FIELDS
    assert not set(got) & moving
    assert got["source"] in ("",) + power_source.SOURCES
    assert got["charge"] in ("",) + power_source.CHARGES


@pytest.mark.parametrize("text", [THIS_MAC, ON_BATTERY, CHARGED, NOT_CHARGING,
                                  FINISHING])
def test_percent_is_an_int(text):
    percent = power_source.parse(text)["percent"]
    assert type(percent) is int


def test_the_time_estimate_does_not_change_the_reading():
    """Two readings a minute apart, the estimate alone moved: the same
    dict, so the section is not news."""
    later = ON_BATTERY.replace("3:12 remaining", "3:11 remaining")
    assert power_source.parse(later) == power_source.parse(ON_BATTERY)


# --- the reader ---------------------------------------------------------------


def _completed(stdout: str, code: int = 0):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr="")


def test_read_runs_pmset_by_absolute_path_with_a_timeout(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return _completed(THIS_MAC)

    monkeypatch.setattr(power_source.subprocess, "run", fake_run)
    assert power_source.read() == power_source.parse(THIS_MAC)
    assert tuple(seen["argv"]) == power_source.PMSET_ARGV
    assert power_source.PMSET_ARGV[0] == "/usr/bin/pmset"
    assert seen["timeout"] == power_source.READ_TIMEOUT_SECONDS
    assert seen["capture_output"] is True
    assert seen["text"] is True


@pytest.mark.parametrize("error", [
    FileNotFoundError("/usr/bin/pmset"),
    subprocess.TimeoutExpired(["/usr/bin/pmset"], 3.0),
    RuntimeError("anything else"),
])
def test_read_that_raises_is_unavailable(monkeypatch, error):
    def fake_run(argv, **kwargs):
        raise error

    monkeypatch.setattr(power_source.subprocess, "run", fake_run)
    assert power_source.read() == {"available": False}


def test_read_with_a_nonzero_exit_is_unavailable(monkeypatch):
    monkeypatch.setattr(power_source.subprocess, "run",
                        lambda argv, **kw: _completed(THIS_MAC, code=1))
    assert power_source.read() == {"available": False}


def test_read_takes_an_explicit_runner():
    assert power_source.read(run=lambda argv, **kw: _completed(DESKTOP)) \
        == power_source.parse(DESKTOP)


# --- the daemon's memo --------------------------------------------------------


class _Counting:
    def __init__(self, *readings):
        self.readings = list(readings)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        value = self.readings[min(self.calls, len(self.readings)) - 1]
        if isinstance(value, Exception):
            raise value
        return value


def _reading(percent, charge="charging"):
    return {"available": True, "present": True, "percent": percent,
            "source": "ac", "charge": charge}


def test_power_snapshot_before_any_read_is_unavailable():
    daemon = BobDaemon()
    assert daemon.power_snapshot() == {"available": False}


def test_the_memo_reads_once_per_interval(monkeypatch):
    daemon = BobDaemon()
    stub = _Counting(_reading(73), _reading(72))
    monkeypatch.setattr(power_source, "read", stub)
    clock = {"t": 1000.0}
    with patch("dark_army_daemon.daemon.time.monotonic",
               side_effect=lambda: clock["t"]):
        assert daemon._power_source_snapshot() == _reading(73)
        assert stub.calls == 1
        clock["t"] += POWER_SOURCE_INTERVAL - 1
        assert daemon._power_source_snapshot() == _reading(73)
        assert stub.calls == 1, "inside the interval reads nothing more"
        clock["t"] += 2
        assert daemon._power_source_snapshot() == _reading(72)
        assert stub.calls == 2, "past the interval reads once more"
    assert daemon.power_snapshot() == _reading(72)


def _two_clocks(clock):
    """Patch both of the memo's clocks off one dict: `mono` and `wall`."""
    return (patch("dark_army_daemon.daemon.time.monotonic",
                  side_effect=lambda: clock["mono"]),
            patch("dark_army_daemon.daemon.time.time",
                  side_effect=lambda: clock["wall"]))


def test_a_sleep_ages_the_memo_by_the_wall_clock(monkeypatch):
    """The monotonic clock stops while the Mac sleeps: a wall clock that
    moved a whole interval re-reads though the monotonic one did not."""
    daemon = BobDaemon()
    stub = _Counting(_reading(73), _reading(40, "discharging"))
    monkeypatch.setattr(power_source, "read", stub)
    clock = {"mono": 1000.0, "wall": 1_700_000_000.0}
    mono, wall = _two_clocks(clock)
    with mono, wall:
        daemon._power_source_snapshot()
        clock["wall"] += POWER_SOURCE_INTERVAL - 1
        daemon._power_source_snapshot()
        assert stub.calls == 1, "inside the interval on both clocks"
        clock["wall"] += 3 * 3600  # the lid was shut for three hours
        assert daemon._power_source_snapshot() == _reading(40, "discharging")
    assert stub.calls == 2


def test_a_wall_clock_that_went_backwards_re_reads(monkeypatch):
    daemon = BobDaemon()
    stub = _Counting(_reading(73), _reading(72))
    monkeypatch.setattr(power_source, "read", stub)
    clock = {"mono": 1000.0, "wall": 1_700_000_000.0}
    mono, wall = _two_clocks(clock)
    with mono, wall:
        daemon._power_source_snapshot()
        clock["wall"] -= 5
        assert daemon._power_source_snapshot() == _reading(72)
    assert stub.calls == 2


def test_a_raising_read_keeps_the_last_reading(monkeypatch):
    daemon = BobDaemon()
    stub = _Counting(_reading(73), RuntimeError("pmset went away"))
    monkeypatch.setattr(power_source, "read", stub)
    clock = {"t": 1000.0}
    with patch("dark_army_daemon.daemon.time.monotonic",
               side_effect=lambda: clock["t"]):
        daemon._power_source_snapshot()
        clock["t"] += POWER_SOURCE_INTERVAL + 1
        assert daemon._power_source_snapshot() == _reading(73)
    assert stub.calls == 2
    assert daemon.power_snapshot() == _reading(73)


def test_power_snapshot_is_a_copy_and_the_memo_is_rebound(monkeypatch):
    daemon = BobDaemon()
    monkeypatch.setattr(power_source, "read", _Counting(_reading(73)))
    daemon._power_source_snapshot()
    held = daemon._power
    snap = daemon.power_snapshot()
    assert snap == held
    assert snap is not daemon._power
    snap["percent"] = 1
    assert daemon._power["percent"] == 73
    # A later read rebinds rather than mutating what the loop may be copying.
    daemon._power_at = None
    monkeypatch.setattr(power_source, "read", _Counting(_reading(70)))
    daemon._power_source_snapshot()
    assert daemon._power is not held
    assert held["percent"] == 73


def test_the_enrich_pass_refreshes_the_memo_and_adds_no_key(monkeypatch):
    daemon = BobDaemon()
    stub = _Counting(_reading(73))
    monkeypatch.setattr(power_source, "read", stub)
    out = daemon._enrich_agent_stubs([])
    assert stub.calls == 1
    assert "power" not in out
    assert daemon.power_snapshot() == _reading(73)


# --- the published section ----------------------------------------------------


def test_the_section_rides_state_and_is_omittable_after_mission(monkeypatch):
    daemon = BobDaemon()
    monkeypatch.setattr(power_source, "read", _Counting(_reading(73)))
    daemon._power_source_snapshot()
    state = ApiServer(daemon).state()
    assert state["power"] == daemon.power_snapshot() == _reading(73)
    sections = api_mod._OMITTABLE_SECTIONS
    assert "power" in sections
    assert sections.index("power") > sections.index("mission")


def test_an_older_daemon_publishes_an_empty_section():
    class _Old:
        _agents_poller = None

    assert ApiServer(_Old()).state()["power"] == {}


def test_a_changed_reading_moves_the_power_digest_alone(monkeypatch):
    daemon = BobDaemon()
    server = ApiServer(daemon)
    stub = _Counting(_reading(73), _reading(72))
    monkeypatch.setattr(power_source, "read", stub)
    daemon._power_source_snapshot()
    before = server.state()
    daemon._power_at = None
    daemon._power_source_snapshot()
    after = server.state()
    # Hold every other section still: only the battery moved.
    for key in api_mod._OMITTABLE_SECTIONS:
        if key != "power":
            after[key] = before[key]
    _, first = api_mod._state_digests(before)
    _, second = api_mod._state_digests(after)
    moved = {k for k in first if first[k] != second.get(k)}
    assert moved == {"power"}


def test_the_same_reading_is_not_news(monkeypatch):
    daemon = BobDaemon()
    server = ApiServer(daemon)
    monkeypatch.setattr(power_source, "read",
                        _Counting(_reading(73), _reading(73)))
    daemon._power_source_snapshot()
    first = api_mod._state_digests(server.state())[1]["power"]
    daemon._power_at = None
    daemon._power_source_snapshot()
    second = api_mod._state_digests(server.state())[1]["power"]
    assert first == second
