"""The ring buffer, and the rates it refuses to invent.

Every assertion here is about *not* answering: the point of a trend is that
somebody acts on it, so a slope fitted to two readings a second apart is worse
than no slope at all.
"""
from dark_army_daemon import samples


def _fill(ring, sid, start, step, ctx=None, cost=None, out=None, n=10):
    for i in range(n):
        metrics = {}
        if ctx is not None:
            metrics["ctx_used_pct"] = ctx(i)
        if cost is not None:
            metrics["cost_usd"] = cost(i)
        if out is not None:
            metrics["ctx_output_tokens"] = out(i)
        ring.add(sid, start + i * step, metrics)


def test_a_steady_climb_becomes_a_rate_and_a_runway():
    ring = samples.SampleRing()
    # 20% to 65%, 3%/min over fifteen minutes.
    _fill(ring, "s1", 1000.0, 60.0, ctx=lambda i: 20.0 + 3.0 * i, n=16)
    trend = ring.trend("s1", 1000.0 + 15 * 60.0)
    assert round(trend["ctx_pct_per_min"], 2) == 3.0
    # 35 points left at 3%/min ≈ 700s.
    assert 650 < trend["ctx_runway_seconds"] < 750


def test_a_series_too_short_to_mean_anything_reports_no_rate():
    ring = samples.SampleRing()
    _fill(ring, "s1", 1000.0, 15.0, ctx=lambda i: 20.0 + i, n=2)
    trend = ring.trend("s1", 1030.0)
    assert trend["samples"] == 2
    assert "ctx_pct_per_min" not in trend
    assert "ctx_runway_seconds" not in trend


def test_a_series_that_has_stopped_reporting_has_no_current_rate():
    """The last slope described a session that is no longer doing that."""
    ring = samples.SampleRing()
    _fill(ring, "s1", 1000.0, 60.0, ctx=lambda i: 20.0 + 3.0 * i, n=10)
    fresh = ring.trend("s1", 1000.0 + 9 * 60.0 + 30)
    stale = ring.trend("s1", 1000.0 + 9 * 60.0 + samples.MAX_AGE_SECONDS + 1)
    assert "ctx_pct_per_min" in fresh
    assert "ctx_pct_per_min" not in stale


def test_a_flat_session_has_a_rate_of_zero_and_no_runway():
    """Zero is an answer; infinity is not. A session that is not filling its
    context has no runway to report, and reporting a vast one would put a
    reassuring number where there is simply no news."""
    ring = samples.SampleRing()
    _fill(ring, "s1", 1000.0, 60.0, ctx=lambda i: 44.0, n=8)
    trend = ring.trend("s1", 1000.0 + 7 * 60.0)
    assert trend["ctx_pct_per_min"] == 0.0
    assert "ctx_runway_seconds" not in trend


def test_a_compaction_does_not_become_a_runway():
    """Context drops by a third when a session compacts. Fitting the whole ring
    means the drop drags the slope negative — no runway — where an endpoint
    difference could have reported a rate that never happened."""
    ring = samples.SampleRing()
    _fill(ring, "s1", 1000.0, 60.0, ctx=lambda i: 60.0 + 2.0 * i, n=8)
    ring.add("s1", 1000.0 + 8 * 60.0, {"ctx_used_pct": 25.0})
    trend = ring.trend("s1", 1000.0 + 8 * 60.0)
    assert trend["ctx_pct_per_min"] < 0
    assert "ctx_runway_seconds" not in trend


def test_readings_closer_than_the_interval_are_dropped():
    ring = samples.SampleRing()
    assert ring.add("s1", 1000.0, {"ctx_used_pct": 10.0}) is True
    assert ring.add("s1", 1000.0 + 1, {"ctx_used_pct": 11.0}) is False
    assert ring.add("s1", 1000.0 + samples.MIN_INTERVAL, {"ctx_used_pct": 12.0}) is True


def test_the_ring_is_bounded():
    ring = samples.SampleRing(capacity=5, min_interval=0.0)
    for i in range(50):
        ring.add("s1", 1000.0 + i, {"ctx_used_pct": float(i)})
    assert ring.trend("s1", 1049.0)["samples"] == 5


def test_a_missing_reading_never_enters_the_series_as_zero():
    """A session whose cost is unreported must not be averaged as free."""
    ring = samples.SampleRing(min_interval=0.0)
    for i in range(6):
        ring.add("s1", 1000.0 + i * 60, {"ctx_used_pct": 10.0 + i})
    trend = ring.trend("s1", 1000.0 + 5 * 60)
    assert "cost_usd_per_hour" not in trend
    assert "ctx_pct_per_min" in trend


def test_cost_and_output_get_their_own_rates():
    ring = samples.SampleRing()
    _fill(ring, "s1", 1000.0, 60.0,
          cost=lambda i: 0.10 * i, out=lambda i: 500.0 * i, n=8)
    trend = ring.trend("s1", 1000.0 + 7 * 60.0)
    assert round(trend["cost_usd_per_hour"], 2) == 6.0
    assert round(trend["output_tokens_per_min"], 1) == 500.0


def test_sessions_that_are_gone_are_forgotten():
    ring = samples.SampleRing()
    ring.add("live", 1000.0, {"ctx_used_pct": 10.0})
    ring.add("dead", 1000.0, {"ctx_used_pct": 10.0})
    ring.keep_only(["live"])
    assert len(ring) == 1
    assert ring.trend("dead", 1000.0)["samples"] == 0
