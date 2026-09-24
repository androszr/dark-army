"""Grok /billing parser and cache. Network is injected; tokens never appear."""
import json
import time

from dark_army_daemon import grok_billing


LIVE = {
    "config": {
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-08-14T21:57:10.999844+00:00",
            "end": "2026-08-21T21:57:10.999844+00:00",
        },
        "creditUsagePercent": 36.0,
        "productUsage": [{"product": "GrokBuild", "usagePercent": 36.0}],
        "isUnifiedBillingUser": True,
        "prepaidBalance": {"val": 0},
        "billingPeriodStart": "2026-08-14T21:57:10.999844+00:00",
        "billingPeriodEnd": "2026-08-21T21:57:10.999844+00:00",
    }
}


def test_parse_weekly_window():
    snap = grok_billing.parse_billing(LIVE, now=1_787_000_000.0)
    assert snap["percent"] == 36.0
    assert snap["cycle"] == "weekly"
    assert snap["stale"] is False
    assert snap["resets_at"] == grok_billing._iso_epoch(
        "2026-08-21T21:57:10.999844+00:00"
    )


def test_parse_marks_stale_after_the_window_ends():
    end = grok_billing._iso_epoch("2026-08-21T21:57:10.999844+00:00")
    snap = grok_billing.parse_billing(LIVE, now=end + 1)
    assert snap["stale"] is True


def test_parse_rejects_an_unknown_shape():
    assert grok_billing.parse_billing({}) == {}
    assert grok_billing.parse_billing({"config": {}}) == {}
    assert grok_billing.parse_billing({"config": {"creditUsagePercent": "full"}}) == {}


def test_parse_falls_back_to_grokbuild_product_percent():
    payload = {
        "config": {
            "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                              "end": "2026-08-21T00:00:00Z"},
            "productUsage": [{"product": "GrokBuild", "usagePercent": 12.5}],
        }
    }
    snap = grok_billing.parse_billing(payload, now=1.0)
    assert snap["percent"] == 12.5


# Live shape after a weekly-limit reset (and the first minutes of a new
# period): the window stays, creditUsagePercent and productUsage go away.
# Captured 16 Aug 2026 from GET /v1/billing?format=credits.
RESET = {
    "config": {
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-08-14T21:57:10.999844+00:00",
            "end": "2026-08-21T21:57:10.999844+00:00",
        },
        "onDemandCap": {"val": 0},
        "onDemandUsed": {"val": 0},
        "isUnifiedBillingUser": True,
        "prepaidBalance": {"val": 0},
        "topUpMethod": "TOP_UP_METHOD_SAVED_PAYMENT_METHOD",
        "billingPeriodStart": "2026-08-14T21:57:10.999844+00:00",
        "billingPeriodEnd": "2026-08-21T21:57:10.999844+00:00",
    }
}


def test_parse_treats_a_reset_window_as_zero_not_unknown():
    snap = grok_billing.parse_billing(RESET, now=1_787_000_000.0)
    assert snap["percent"] == 0.0
    assert snap["cycle"] == "weekly"
    assert snap["stale"] is False
    assert snap["resets_at"] == grok_billing._iso_epoch(
        "2026-08-21T21:57:10.999844+00:00"
    )


def test_parse_accepts_usage_percent_under_another_key():
    snap = grok_billing.parse_billing(
        {"config": {"usagePercent": "18.5",
                    "billingPeriodEnd": "2026-08-21T00:00:00Z"}},
        now=1.0,
    )
    assert snap["percent"] == 18.5


def test_get_snapshot_a_reset_overwrites_the_held_percent():
    """A 200 with the period but no fill is a reading, not a failed fetch.

    Holding the previous 36% after a reset would be the more dangerous lie.
    """
    grok_billing.reset_cache()
    grok_billing.get_snapshot(
        now=1.0, fetch=lambda: grok_billing.parse_billing(LIVE, now=1.0),
    )
    snap = grok_billing.get_snapshot(
        now=1.0 + grok_billing.TTL_SECONDS + 1,
        fetch=lambda: grok_billing.parse_billing(RESET, now=2.0),
    )
    assert snap["percent"] == 0.0


def test_read_token_picks_an_unexpired_key(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({
        "https://auth.x.ai::old": {
            "key": "expired-token",
            "expires_at": "2020-01-01T00:00:00Z",
        },
        "https://auth.x.ai::live": {
            "key": "live-token",
            "expires_at": "2099-01-01T00:00:00Z",
        },
    }))
    assert grok_billing.read_token(path) == "live-token"


def test_read_token_missing_file(tmp_path):
    assert grok_billing.read_token(tmp_path / "nope.json") == ""


def test_get_snapshot_uses_the_injected_fetch_and_caches():
    grok_billing.reset_cache()
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return grok_billing.parse_billing(LIVE, now=1_787_000_000.0)

    first = grok_billing.get_snapshot(now=1_787_000_000.0, fetch=fetch)
    second = grok_billing.get_snapshot(now=1_787_000_010.0, fetch=fetch)
    assert first["percent"] == 36.0
    assert second == first
    assert calls["n"] == 1


def test_get_snapshot_keeps_the_last_reading_when_a_refresh_fails():
    grok_billing.reset_cache()
    grok_billing.get_snapshot(
        now=1.0, fetch=lambda: grok_billing.parse_billing(LIVE, now=1.0),
    )
    held = grok_billing.get_snapshot(now=1.0 + grok_billing.TTL_SECONDS + 1,
                                     fetch=lambda: {})
    assert held["percent"] == 36.0
