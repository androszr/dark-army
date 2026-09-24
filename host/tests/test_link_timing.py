# host/tests/test_link_timing.py
"""The pure link-timing module: percentiles, the window that closes on the
clock handed in and not before, the bounded ring, per-(door, device)
isolation, the `size` hop, and the closed key set pinned equal to
`access_log.HOP_KEYS` across the two modules."""

from __future__ import annotations

from pathlib import Path

from dark_army_daemon import access_log, link_timing
from dark_army_daemon.link_timing import (
    HOP_KEYS, HOPS, MAX_SAMPLES, ROLLUP_SECONDS, LinkTimer, percentile,
)


# --- percentile --------------------------------------------------------------------


def test_percentile_of_nothing_is_zero():
    assert percentile([], 50) == 0.0
    assert percentile([], 90) == 0.0


def test_percentile_of_one_sample_is_that_sample():
    assert percentile([2.5], 50) == 2.5
    assert percentile([2.5], 90) == 2.5
    assert percentile([2.5], 100) == 2.5


def test_percentile_of_two_is_the_lower_at_p50_and_the_upper_at_p90():
    assert percentile([1.0, 3.0], 50) == 1.0
    assert percentile([1.0, 3.0], 90) == 3.0
    assert percentile([3.0, 1.0], 50) == 1.0


def test_percentile_nearest_rank_over_ten_and_ties():
    values = [float(i) for i in range(1, 11)]
    assert percentile(values, 50) == 5.0
    assert percentile(values, 90) == 9.0
    assert percentile(values, 100) == 10.0
    assert percentile([4.0] * 7, 50) == 4.0
    assert percentile([4.0] * 7, 90) == 4.0
    # A clamped share, never an index error.
    assert percentile(values, 0) == 1.0
    assert percentile(values, 250) == 10.0
    assert percentile(values, "x") == 5.0


# --- the window ----------------------------------------------------------------------


def _add(timer, now, **hops):
    return timer.add("relay", "dev-1", "state", hops, 200, 80_000, now)


def test_a_rollup_closes_on_the_window_and_not_before():
    timer = LinkTimer()
    assert _add(timer, 1000.0, dwell=1.0, run=0.1) is None
    assert _add(timer, 1000.0 + ROLLUP_SECONDS - 1, dwell=3.0, run=0.3) is None
    closed = _add(timer, 1000.0 + ROLLUP_SECONDS, dwell=9.0, run=0.9)
    assert closed is not None
    assert closed["door"] == "relay"
    assert closed["device_id"] == "dev-1"
    assert closed["count"] == 2
    assert closed["window_seconds"] == int(ROLLUP_SECONDS)
    assert closed["hops"]["n"] == 2
    assert closed["hops"]["dwell_p50"] == 1.0
    assert closed["hops"]["dwell_p90"] == 3.0
    assert closed["hops"]["dwell_max"] == 3.0
    assert closed["hops"]["run_max"] == 0.3
    # The sample that closed the window opened the next one.
    fresh = timer.rollup("relay", "dev-1", 1000.0 + ROLLUP_SECONDS + 5)
    assert fresh["count"] == 1
    assert fresh["hops"]["dwell_p50"] == 9.0
    assert timer.rollup("relay", "dev-1", 2000.0) is None


def test_the_ring_keeps_the_newest_samples_and_the_count_keeps_going():
    timer = LinkTimer()
    for i in range(MAX_SAMPLES + 40):
        _add(timer, 1000.0 + i * 0.01, dwell=float(i))
    closed = timer.rollup("relay", "dev-1", 1100.0)
    assert closed["count"] == MAX_SAMPLES + 40
    assert closed["hops"]["n"] == MAX_SAMPLES
    # The forty oldest went: the smallest dwell left is 40.
    assert closed["hops"]["dwell_p50"] >= 40.0
    assert closed["hops"]["dwell_max"] == float(MAX_SAMPLES + 39)


def test_windows_are_per_door_and_device():
    timer = LinkTimer()
    timer.add("relay", "dev-1", "state", {"dwell": 1.0}, 200, 10, 1000.0)
    timer.add("relay", "dev-2", "state", {"dwell": 5.0}, 200, 10, 1000.0)
    timer.add("lan", "dev-1", "state", {"open": 0.01}, 200, 10, 1000.0)
    assert timer.windows() == 3
    one = timer.rollup("relay", "dev-1", 1001.0)
    assert one["count"] == 1 and one["hops"]["dwell_p50"] == 1.0
    two = timer.rollup("relay", "dev-2", 1001.0)
    assert two["count"] == 1 and two["hops"]["dwell_p50"] == 5.0
    home = timer.rollup("lan", "dev-1", 1001.0)
    assert home["door"] == "lan"
    assert home["hops"]["open_p50"] == 0.01
    assert home["hops"]["dwell_p50"] == 0.0  # the LAN door has no dwell
    assert timer.windows() == 0


def test_the_size_hop_is_the_reply_size():
    timer = LinkTimer()
    timer.add("relay", "dev-1", "state", {"run": 0.1}, 200, 81_000, 1000.0)
    timer.add("relay", "dev-1", "state", {"run": 0.1}, 200, 150, 1001.0)
    closed = timer.rollup("relay", "dev-1", 1002.0)
    assert closed["hops"]["size_p50"] == 150.0
    assert closed["hops"]["size_max"] == 81_000.0
    # A size that is not a number is simply no size hop.
    timer.add("relay", "dev-1", "state", {"run": 0.1}, 200, "big", 1003.0)
    closed = timer.rollup("relay", "dev-1", 1004.0)
    assert closed["hops"]["size_max"] == 0.0


def test_an_unknown_hop_key_and_a_bad_value_are_dropped():
    timer = LinkTimer()
    timer.add("relay", "dev-1", "state",
              {"dwell": 2.0, "mailbox_url": 9.0, "run": "slow",
               "answer": float("nan"), "open": -3.0},
              "200", None, 1000.0)
    closed = timer.rollup("relay", "dev-1", 1001.0)
    assert set(closed["hops"]) == set(HOP_KEYS)
    assert closed["hops"]["dwell_p50"] == 2.0
    assert closed["hops"]["run_p50"] == 0.0
    assert closed["hops"]["answer_p50"] == 0.0
    assert closed["hops"]["open_p50"] == 0.0  # clamped at zero, never negative
    assert "mailbox_url_p50" not in closed["hops"]


def test_hops_that_are_not_a_dict_make_an_empty_sample():
    timer = LinkTimer()
    assert timer.add("lan", "dev-1", "usage", None, 200, 0, 1000.0) is None
    assert timer.add("lan", "dev-1", "usage", ["dwell"], 200, 0, 1000.0) is None
    closed = timer.rollup("lan", "dev-1", 1001.0)
    assert closed["count"] == 2
    assert closed["hops"]["n"] == 2
    assert all(closed["hops"][k] == 0.0 for k in HOP_KEYS if k != "n")


def test_a_rollup_carries_every_hop_key_and_no_other():
    timer = LinkTimer()
    timer.add("relay", "dev-1", "action", {"dwell": 1.0}, 200, 5, 1000.0)
    closed = timer.rollup("relay", "dev-1", 1001.0)
    assert tuple(closed["hops"]) == HOP_KEYS
    assert set(closed) == {"door", "device_id", "count", "window_seconds", "hops"}
    for value in closed["hops"].values():
        assert isinstance(value, (int, float))


def test_hop_keys_are_pinned_across_the_two_modules():
    """`access_log.HOP_KEYS` is stated literally in its own file and must
    read exactly what `LinkTimer.rollup` emits — the access log refuses a
    key outside its tuple, so a hop added here alone would refuse every
    rollup."""
    assert access_log.HOP_KEYS == HOP_KEYS
    assert HOPS == ("dwell", "hold", "open", "run", "answer", "seal", "size")
    timer = LinkTimer()
    timer.add("relay", "dev-1", "state", {h: 1.0 for h in HOPS}, 200, 1, 0.0)
    closed = timer.rollup("relay", "dev-1", 1.0)
    assert tuple(closed["hops"]) == access_log.HOP_KEYS


def test_the_constants_and_no_platform_branch():
    assert ROLLUP_SECONDS == 600.0
    assert MAX_SAMPLES == 512
    src = Path(link_timing.__file__).read_text()
    assert "sys.platform" not in src
    assert "platform.system" not in src
    assert "import asyncio" not in src
    assert "import time" not in src
