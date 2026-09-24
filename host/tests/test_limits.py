# host/tests/test_limits.py
"""The rate-limit bars: drawing them from the statusline, and from two caches.

Fixture shapes are copied from real files — `cachedUsageUtilization` as written by
Claude Code 2.1.212 (including the two vocabularies for one window: `kind:
weekly_all` in the limits list, `bar: seven_day` in the promo notice), and
`~/.claude/limits-cache.json` as written by 2.1.220, which no longer writes the
first one at all.
"""

import json

import pytest

from dark_army_daemon import limits


def _config(limit_entries=None, promos=None, fetched_at_ms=1_785_071_303_936):
    entries = [
        {"kind": "session", "group": "session", "percent": 25, "severity": "normal",
         "resets_at": "2026-07-26T16:30:00.675225+00:00", "scope": None,
         "is_active": True},
        {"kind": "weekly_all", "group": "weekly", "percent": 19, "severity": "normal",
         "resets_at": "2026-08-01T23:59:59.675247+00:00", "scope": None,
         "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 11,
         "severity": "normal", "resets_at": "2026-08-01T23:59:59.675481+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
         "is_active": False},
    ] if limit_entries is None else limit_entries
    return {
        "cachedUsageUtilization": {
            "fetchedAtMs": fetched_at_ms,
            "utilization": {"limits": entries},
        },
        "cachedGrowthBookFeatures": {
            "tengu_rate_limit_promo_notices": promos if promos is not None else [
                {"bar": "seven_day", "variant": "claude",
                 "text": "+50% weekly limits promo through Aug 19 · clau.de/cc-50-promo"},
            ],
        },
    }


def _limits_cache(five_hour=None, ts=1_785_094_000_000):
    """`~/.claude/limits-cache.json` as 2.1.220 writes it: a reset instant in epoch
    *seconds*, an overage flag, and no percentage anywhere."""
    return {"ts": ts, "data": {
        "rate_limits": {"five_hour": {
            "status": "allowed", "resets_at": 1_785_102_000,
            "is_using_overage": False, "overage_status": "allowed",
        } if five_hour is None else five_hour},
        "fetched_at": ts,
    }}


@pytest.fixture
def config_path(tmp_path):
    def write(config):
        path = tmp_path / ".claude.json"
        path.write_text(json.dumps(config))
        return path
    return write


@pytest.fixture
def limits_path(tmp_path):
    def write(payload):
        path = tmp_path / "limits-cache.json"
        path.write_text(json.dumps(payload))
        return path
    return write


@pytest.fixture(autouse=True)
def _no_real_limits_cache(tmp_path, monkeypatch):
    """Point the module's default at nothing, for every test that does not name a
    file of its own. Otherwise the suite reads the developer's own
    `~/.claude/limits-cache.json` and passes or fails by what is in it."""
    monkeypatch.setattr(limits, "CLAUDE_LIMITS_CACHE_PATH",
                        tmp_path / "absent" / "limits-cache.json")


# --- reading the cache -------------------------------------------------------


def test_the_three_bars_are_read_in_the_order_the_source_shows_them(config_path):
    """Session first, then the weekly windows — the order the reader already
    learned in the terminal."""
    snap = limits.snapshot(path=config_path(_config()))
    assert [(b["title"], b["percent"]) for b in snap["bars"]] == [
        ("Current session", 25),
        ("Current week (all models)", 19),
        ("Current week (Fable)", 11),
    ]


def test_a_scoped_window_is_titled_from_its_model(config_path):
    """The per-model weekly limit exists nowhere else on the machine — not in the
    statusline, not in limits-cache.json — so its label has to come from here."""
    snap = limits.snapshot(path=config_path(_config()))
    assert snap["bars"][2]["kind"] == "weekly_scoped"


def test_reset_times_become_epoch_seconds(config_path):
    """The cache writes ISO strings and the panel formats epochs in the reader's
    own timezone; one of the two has to give."""
    snap = limits.snapshot(path=config_path(_config()))
    assert snap["bars"][0]["resets_at"] == pytest.approx(1_785_083_400.675, abs=1)


def test_the_fetch_time_is_reported_so_a_stale_bar_can_say_so(config_path):
    snap = limits.snapshot(path=config_path(_config()))
    assert snap["fetched_at"] == pytest.approx(1_785_071_303.936, abs=1)


def test_a_bar_without_a_percentage_is_dropped(config_path):
    """A window Claude Code knows about but has no figure for is not a bar at 0%."""
    snap = limits.snapshot(path=config_path(_config(limit_entries=[
        {"kind": "session", "percent": None, "resets_at": None},
        {"kind": "weekly_all", "group": "weekly", "percent": 3, "resets_at": None},
    ])))
    assert [b["kind"] for b in snap["bars"]] == ["weekly_all"]


def test_zero_percent_is_a_bar_and_not_a_missing_one(config_path):
    """"You have used none of it" and "we were never told" are different, and only
    one of them is a reason to hide the bar."""
    snap = limits.snapshot(path=config_path(_config(limit_entries=[
        {"kind": "session", "percent": 0, "resets_at": None}])))
    assert snap["available"] is True and snap["bars"][0]["percent"] == 0


# --- promo notices -----------------------------------------------------------


def test_a_promo_is_attached_to_the_bar_it_names(config_path):
    """The notice says `seven_day` while the bar says `weekly_all`; unmatched, the
    line would silently never print."""
    snap = limits.snapshot(path=config_path(_config()))
    weekly = next(b for b in snap["bars"] if b["kind"] == "weekly_all")
    assert weekly["promos"] == [
        "+50% weekly limits promo through Aug 19 · clau.de/cc-50-promo"]
    assert all(not b["promos"] for b in snap["bars"] if b["kind"] != "weekly_all")


def test_a_promo_for_an_unknown_bar_is_kept_rather_than_dropped(config_path):
    """Naming moves on; "+50% weekly limits" is still worth reading."""
    snap = limits.snapshot(path=config_path(_config(promos=[
        {"bar": "some_future_window", "text": "+100% for a bit"}])))
    assert snap["notices"] == ["+100% for a bit"]


def test_a_promo_with_no_text_is_ignored(config_path):
    snap = limits.snapshot(path=config_path(_config(promos=[
        {"bar": "seven_day", "text": "  "}, {"bar": "seven_day"}])))
    assert snap["notices"] == []
    assert all(not b["promos"] for b in snap["bars"])


# --- merging the statusline --------------------------------------------------


def test_the_statusline_wins_for_the_windows_it_reports(config_path):
    """It is by construction the figure as of the last conversation update, so it
    is at least as new as anything the cache holds."""
    snap = limits.snapshot({"five_hour_pct": 31, "seven_day_pct": 22},
                           path=config_path(_config()))
    session, weekly, scoped = snap["bars"]
    assert (session["percent"], session["source"]) == (31, "statusline")
    assert (weekly["percent"], weekly["source"]) == (22, "statusline")
    # No statusline field exists for a per-model window, so it keeps the cache's.
    assert (scoped["percent"], scoped["source"]) == (11, "cache")


def test_a_merged_bar_takes_the_statusline_reset_time_too(config_path):
    """The percentage is the fraction of *that* window used, so the clock comes
    with it. The cache in this fixture was written before the five-hour window
    rolled over and still names 18:30 local; the statusline knows 23:40, and a
    fresh number under a dead label is what this whole module is about."""
    snap = limits.snapshot(
        {"five_hour_pct": 31, "five_hour_resets_at": 1_785_102_000},
        path=config_path(_config()))
    assert snap["bars"][0]["resets_at"] == 1_785_102_000


def test_a_merged_bar_keeps_the_cached_reset_time_when_the_statusline_has_none(
        config_path):
    """Older Claude Code builds report a percentage with no reset instant. Half a
    reading still beats none — take the number, keep the cache's clock."""
    snap = limits.snapshot({"five_hour_pct": 31}, path=config_path(_config()))
    assert snap["bars"][0]["resets_at"] == pytest.approx(1_785_083_400.675, abs=1)


def test_a_window_whose_reset_has_passed_is_flagged():
    """A percentage of a window that has since restarted at zero is flagged, not
    silently redrawn as a level. `now` is passed rather than taken from the clock
    — a staleness rule tested against wall time is a rule that starts failing at
    a particular hour."""
    bars = limits.mark_freshness(
        [{"kind": "session", "percent": 25, "resets_at": 1_785_083_400},
         {"kind": "weekly_all", "percent": 19, "resets_at": 1_785_628_800}],
        fetched_at=1_785_071_303, now=1_785_094_000)
    assert [b["stale"] for b in bars] == [True, False]


def test_a_bar_with_no_reset_time_is_not_stale():
    """Unknown is not expired: a window we were never given a clock for cannot be
    asserted to have run out."""
    bars = limits.mark_freshness([{"kind": "session", "percent": 25,
                                   "resets_at": None}],
                                 fetched_at=None, now=1_785_094_000)
    assert bars[0]["stale"] is False


def test_a_fresh_statusline_clears_a_stale_cached_window(config_path):
    """The bug, end to end. The cache holds a session window that reset at 18:30;
    a statusline reporting the 23:40 window replaces the number *and* the clock,
    so the bar stops claiming a reset that has already happened."""
    snap = limits.snapshot(
        {"five_hour_pct": 17, "five_hour_resets_at": 4_102_444_800,
         "received_at": 1_785_094_000},
        path=config_path(_config()))
    assert (snap["bars"][0]["percent"], snap["bars"][0]["stale"]) == (17, False)


def test_a_live_reading_is_dated_by_its_statusline_not_the_cache(config_path):
    """`as_of` is per bar, because on one screen the two account-wide windows can
    be seconds old while the per-model one is hours old — and a bar with no age
    on it reads as current."""
    snap = limits.snapshot(
        {"five_hour_pct": 31, "received_at": 1_785_094_000},
        path=config_path(_config()))
    assert snap["bars"][0]["as_of"] == 1_785_094_000
    # Untouched by the statusline, so it is dated by the cache's own fetch time.
    assert snap["bars"][2]["as_of"] == pytest.approx(1_785_071_303.936, abs=1)


def test_a_missing_statusline_figure_changes_nothing(config_path):
    snap = limits.snapshot({"seven_day_pct": 22}, path=config_path(_config()))
    assert (snap["bars"][0]["percent"], snap["bars"][0]["source"]) == (25, "cache")


def test_a_non_numeric_statusline_figure_is_ignored(config_path):
    """A string sneaking in would render as a bar of width "unknown%"."""
    snap = limits.snapshot({"five_hour_pct": "31"}, path=config_path(_config()))
    assert snap["bars"][0]["percent"] == 25


# --- the statusline as a source, not an overlay -------------------------------


def test_the_statusline_draws_both_bars_with_no_usage_cache_at_all(tmp_path):
    """The bug, end to end, on a current install. Claude Code 2.1.220 stopped
    writing `cachedUsageUtilization`, and a merge that could only *overwrite*
    needed that key to have named the window first — so the Usage view drew no
    bars while holding two live percentages the header rendered fine."""
    snap = limits.snapshot(
        {"five_hour_pct": 4, "five_hour_resets_at": 4_102_444_800,
         "seven_day_pct": 31, "seven_day_resets_at": 4_102_444_800,
         "received_at": 1_785_094_000},
        path=tmp_path / "nope.json")
    assert snap["available"] is True
    assert [(b["title"], b["percent"], b["source"]) for b in snap["bars"]] == [
        ("Current session", 4, "statusline"),
        ("Current week (all models)", 31, "statusline"),
    ]


def test_a_synthesised_bar_carries_its_own_clock_and_age(tmp_path):
    """Same reading, so the same rule as a merged bar: the reset instant and the
    percentage travel together, and the bar is dated by the tick it came from."""
    snap = limits.snapshot(
        {"five_hour_pct": 4, "five_hour_resets_at": 4_102_444_800,
         "received_at": 1_785_094_000},
        path=tmp_path / "nope.json")
    bar = snap["bars"][0]
    assert (bar["resets_at"], bar["as_of"], bar["stale"]) == (
        4_102_444_800, 1_785_094_000, False)


def test_a_synthesised_bar_is_flagged_when_its_window_has_closed(tmp_path):
    """Emitted bars go through the same freshness pass as read ones — a live
    source is not the same as a current window."""
    snap = limits.snapshot({"five_hour_pct": 4,
                            "five_hour_resets_at": 1_785_083_400},
                           path=tmp_path / "nope.json")
    assert snap["bars"][0]["stale"] is True


def test_a_window_the_statusline_is_silent_about_gets_no_bar(tmp_path):
    """Half a statusline is half the bars. Inventing the other at 0% would be
    reporting a figure nobody gave us."""
    snap = limits.snapshot({"five_hour_pct": 4}, path=tmp_path / "nope.json")
    assert [b["kind"] for b in snap["bars"]] == ["session"]


def test_the_scoped_bar_cannot_be_synthesised(tmp_path):
    """No statusline field exists for the per-model weekly window, so on a machine
    without the usage cache that bar is simply gone — not drawn from a guess."""
    snap = limits.snapshot({"five_hour_pct": 4, "seven_day_pct": 31},
                           path=tmp_path / "nope.json")
    assert all(b["kind"] != "weekly_scoped" for b in snap["bars"])


def test_a_bar_the_cache_already_has_is_overwritten_and_not_duplicated(config_path):
    """One window, one bar. The emitting half must not fire for a kind the cache
    laid out — two "Current session" rows disagreeing by a percentage point is
    worse than either."""
    snap = limits.snapshot({"five_hour_pct": 31, "seven_day_pct": 22},
                           path=config_path(_config()))
    assert [b["kind"] for b in snap["bars"]] == [
        "session", "weekly_all", "weekly_scoped"]


def test_a_non_numeric_statusline_figure_synthesises_nothing(tmp_path):
    """The emitting path applies the same filter as the overwriting one, or a
    string arrives as a bar of width "unknown%" by the other door."""
    snap = limits.snapshot({"five_hour_pct": "4"}, path=tmp_path / "nope.json")
    assert snap["bars"] == [] and snap["available"] is False


def test_a_synthesised_bar_takes_a_promo_like_any_other(config_path):
    """The promo lives in a feature flag, not in the usage cache, so it outlives
    the bars — and a bar this module drew itself is still the bar it belongs
    under."""
    snap = limits.snapshot({"seven_day_pct": 31},
                           path=config_path(_config(limit_entries=[])))
    assert snap["bars"][0]["promos"] == [
        "+50% weekly limits promo through Aug 19 · clau.de/cc-50-promo"]


# --- limits-cache.json, which fills gaps and draws nothing --------------------


def test_the_thin_cache_is_keyed_by_bar_kind(limits_path):
    """`five_hour` in the file, `session` on the bar. Reading it in the caller's
    vocabulary is the difference between filling a gap and filling nothing."""
    cache = limits.read_limits_cache(limits_path(_limits_cache()))
    assert cache["windows"]["session"]["resets_at"] == 1_785_102_000
    assert cache["fetched_at"] == pytest.approx(1_785_094_000, abs=1)


def test_it_supplies_a_reset_time_nothing_else_had(limits_path):
    """An older statusline reports a percentage with no clock, and with no usage
    cache there is nothing else left to take one from."""
    bars = limits.apply_limits_cache(
        [{"kind": "session", "percent": 4, "resets_at": None}],
        limits.read_limits_cache(limits_path(_limits_cache()))["windows"],
        now=1_785_094_000)
    assert bars[0]["resets_at"] == 1_785_102_000


def test_it_never_overrules_a_reset_time_that_is_already_there(limits_path):
    """It has no percentage, so it cannot be the reading — only the gaps in one.
    A clock from here under a statusline's number is the mismatch this module
    exists to prevent."""
    bars = limits.apply_limits_cache(
        [{"kind": "session", "percent": 4, "resets_at": 4_102_444_800}],
        limits.read_limits_cache(limits_path(_limits_cache()))["windows"],
        now=1_785_094_000)
    assert bars[0]["resets_at"] == 4_102_444_800


def test_an_entry_whose_window_has_closed_contributes_nothing(limits_path):
    """The copy on a real machine was seven weeks old and named a five-hour window
    from June. Hanging that reset on a live bar would flag it stale and have the
    panel announce that the window being actively spent ended weeks ago."""
    cache = limits.read_limits_cache(limits_path(_limits_cache(five_hour={
        "status": "allowed", "resets_at": 1_780_911_000,
        "is_using_overage": True})))
    bars = limits.apply_limits_cache(
        [{"kind": "session", "percent": 4, "resets_at": None}],
        cache["windows"], now=1_785_094_000)
    assert bars[0]["resets_at"] is None and "overage" not in bars[0]


def test_an_entry_with_no_reset_instant_cannot_be_dated_or_trusted(limits_path):
    cache = limits.read_limits_cache(limits_path(_limits_cache(five_hour={
        "status": "allowed", "is_using_overage": True})))
    bars = limits.apply_limits_cache([{"kind": "session", "percent": 4}],
                                     cache["windows"], now=1_785_094_000)
    assert "overage" not in bars[0]


def test_overage_is_the_one_thing_only_this_file_reports(limits_path):
    """At 100% the account keeps working and starts billing, which no percentage
    on the bar says out loud."""
    cache = limits.read_limits_cache(limits_path(_limits_cache(five_hour={
        "status": "allowed_warning", "resets_at": 4_102_444_800,
        "is_using_overage": True})))
    bars = limits.apply_limits_cache([{"kind": "session", "percent": 98}],
                                     cache["windows"], now=1_785_094_000)
    assert bars[0]["overage"] is True


def test_a_bar_not_on_overage_says_nothing_rather_than_false(limits_path):
    """Absent reads as "not on overage" wherever it is consumed, and this file
    only sometimes earns the right to make the claim at all."""
    bars = limits.apply_limits_cache(
        [{"kind": "session", "percent": 4}],
        limits.read_limits_cache(limits_path(_limits_cache()))["windows"],
        now=1_785_094_000)
    assert "overage" not in bars[0]


def test_the_thin_cache_fills_a_synthesised_bar_end_to_end(tmp_path, limits_path):
    """All three sources at once, on a current install: no usage cache, a
    statusline with a percentage but no clock, and the reset instant coming from
    the only file that still has one."""
    snap = limits.snapshot({"five_hour_pct": 4}, path=tmp_path / "nope.json",
                           limits_path=limits_path(_limits_cache(five_hour={
                               "status": "allowed", "resets_at": 4_102_444_800})))
    assert (snap["bars"][0]["percent"], snap["bars"][0]["resets_at"]) == (
        4, 4_102_444_800)


def test_a_missing_thin_cache_is_not_an_error(tmp_path):
    assert limits.read_limits_cache(tmp_path / "nope.json") == {
        "windows": {}, "fetched_at": None}


def test_a_corrupt_thin_cache_is_not_an_error(limits_path, tmp_path):
    path = tmp_path / "limits-cache.json"
    path.write_text("{ not json either")
    assert limits.read_limits_cache(path)["windows"] == {}


def test_an_unknown_window_name_in_the_thin_cache_is_skipped(limits_path):
    """A window naming has moved on before. Skipping it beats keying a bar off a
    name no bar has."""
    cache = limits.read_limits_cache(limits_path({"ts": 1, "data": {
        "rate_limits": {"some_future_window": {"resets_at": 4_102_444_800}}}}))
    assert cache["windows"] == {}


# --- somebody else's file ----------------------------------------------------


def test_a_missing_config_is_not_an_error(tmp_path):
    """The normal case on a machine where Claude Code has not run yet."""
    snap = limits.snapshot(path=tmp_path / "nope.json")
    assert snap == {"bars": [], "notices": [], "fetched_at": None, "available": False}


def test_a_corrupt_config_is_not_an_error(tmp_path):
    """~100 KB of state we do not own, written by another process while we read.
    No bar is worth taking the daemon down for."""
    path = tmp_path / ".claude.json"
    path.write_text("{ this is not json")
    assert limits.snapshot(path=path)["available"] is False


def test_a_config_without_the_usage_cache_yields_no_bars(tmp_path):
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps({"userID": "x", "projects": {}}))
    assert limits.snapshot(path=path)["bars"] == []


def test_non_finite_numbers_are_not_numbers():
    """NaN and infinity are floats, and so pass every isinstance() guard downstream.

    They reach `menu_format.usage_text`, which does int(percent) — ValueError on
    NaN, OverflowError on inf — from the top of the 5×/s menu-bar render timer,
    above its try/except. Rejecting them at the file boundary is the cheap place.
    """
    assert limits._num(float("nan")) is None
    assert limits._num(float("inf")) is None
    assert limits._num(float("-inf")) is None
    assert limits._num(25) == 25
    assert limits._num(0) == 0


def test_a_config_carrying_nan_still_renders(tmp_path):
    """json.load parses the bare NaN literal by default, so this is reachable."""
    from dark_army_menubar import menu_format
    path = tmp_path / ".claude.json"
    path.write_text(
        '{"cachedUsageUtilization":{"utilization":{"limits":'
        '[{"kind":"session","percent":NaN,"resets_at":null}]}}}'
    )
    snap = limits.snapshot(path=path)
    # The render path is what must not raise; the figure itself is simply unknown.
    assert menu_format.usage_text(snap) == ""
    assert menu_format.limit_percent(snap) is None


def test_this_module_still_draws_the_cached_scoped_bar(config_path, tmp_path):
    """`claude_usage` fetches the scoped window live and merges over this
    module's output — it does not replace it, and nothing here calls it.

    The live half is what freshens the figure; this half is what the user still
    sees when the fetch fails, which is every offline moment and every one where
    the keychain says no. So the cached bar has to keep coming out of
    `snapshot()` on its own, dated by the cache's own `fetchedAtMs` — a
    regression here would be invisible while the network is up.
    """
    path = config_path(_config())
    snap = limits.snapshot(None, path, tmp_path / "absent.json")

    scoped = [b for b in snap["bars"] if b["kind"] == "weekly_scoped"]
    assert len(scoped) == 1
    assert scoped[0]["title"] == "Current week (Fable)"
    assert scoped[0]["percent"] == 11
    assert scoped[0]["source"] == "cache"
    assert scoped[0]["as_of"] == 1_785_071_303_936 / 1000
    # ...and the dependency runs one way only: the merge is the caller's, in
    # `api_server`, so this module keeps working with `claude_usage` deleted.
    import inspect
    source = inspect.getsource(limits)
    assert "import claude_usage" not in source
    assert "from .claude_usage" not in source
    assert "claude_usage.merge_snapshot" not in source
    assert "claude_usage.get_snapshot" not in source

