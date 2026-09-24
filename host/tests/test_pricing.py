# host/tests/test_pricing.py
"""Fallback cost estimation: rate resolution, cache multipliers, unknown models."""

import json

import pytest

from dark_army_daemon import pricing
from dark_army_daemon.pricing import Rate, cost_usd, resolve_rate


@pytest.fixture(autouse=True)
def _isolate_overrides(tmp_path, monkeypatch):
    """Each test starts from the built-in table with overrides unread."""
    monkeypatch.setattr(pricing, "PRICING_OVERRIDE_PATH", tmp_path / "pricing.json")
    monkeypatch.setattr(pricing, "_overrides_loaded", False)
    monkeypatch.setattr(pricing, "RATES", dict(pricing.RATES))


# --- rate resolution ---------------------------------------------------------


def test_exact_model_id():
    assert resolve_rate("claude-opus-4-8").input == 5.00


def test_datestamped_id_resolves_by_prefix():
    """Transcripts carry ids like claude-haiku-4-5-20251001; a table entry per
    datestamp would go stale by construction."""
    assert resolve_rate("claude-haiku-4-5-20251001").output == 5.00


def test_context_suffix_resolves_by_prefix():
    assert resolve_rate("claude-opus-4-8[1m]").input == 5.00


def test_fable_5_1_has_its_own_exact_row():
    """The 5-1 id is priced from its own entry, not a prefix lookalike."""
    assert "claude-fable-5-1" in pricing.RATES
    rate = resolve_rate("claude-fable-5-1")
    assert (rate.input, rate.output) == (10.00, 50.00)


def test_datestamped_fable_5_1_resolves_through_its_own_row():
    """Dict order is semantic: the prefix tier takes the first startswith hit,
    so the 5-1 row must sit above "claude-fable-5" or a datestamped 5-1 id
    would fall through to the 5 row."""
    keys = list(pricing.RATES)
    assert keys.index("claude-fable-5-1") < keys.index("claude-fable-5")
    assert resolve_rate("claude-fable-5-1-20261001") is pricing.RATES[
        "claude-fable-5-1"]


def test_fable_family_alias_means_the_latest_release():
    assert pricing.FAMILY_RATES["fable"] is pricing.RATES["claude-fable-5-1"]


def test_unknown_point_release_falls_back_to_family():
    """A model that ships after this build should be priced approximately rather
    than not at all — still labelled an estimate either way."""
    rate = resolve_rate("claude-opus-9-9")
    assert rate is not None and rate.input == 5.00


def test_case_and_whitespace_tolerant():
    assert resolve_rate("  Claude-Opus-4-8 ").input == 5.00


@pytest.mark.parametrize("model", [None, "", "gpt-4", "llama-3", "local-model"])
def test_foreign_models_are_unpriced(model):
    assert resolve_rate(model) is None


# --- cost arithmetic ---------------------------------------------------------


def test_input_and_output_priced_per_million():
    cost = cost_usd("claude-opus-4-8", input_tokens=1_000_000,
                    output_tokens=1_000_000)
    assert cost == pytest.approx(5.00 + 25.00)


def test_cache_read_is_a_tenth_of_input():
    """Cache rates are multipliers on the input rate, not separate figures — so a
    new model cannot be internally inconsistent."""
    cost = cost_usd("claude-opus-4-8", cache_read=1_000_000)
    assert cost == pytest.approx(0.50)


def test_fable_cache_reads_are_priced_at_25_cents_per_mtok():
    """Fable 5 / 5.1 (and Mythos) price cache reads at $0.25/MTok — 0.025x their
    $10 input rate. The flat 0.10x rule overstated their cache reads 4x."""
    for model in ("claude-fable-5", "claude-fable-5-1",
                  "claude-mythos-5", "claude-mythos-5-1"):
        assert cost_usd(model, cache_read=1_000_000) == pytest.approx(0.25), model


def test_fable_cache_writes_keep_the_standard_multipliers():
    """Only the read rate is special; writes stay 1.25x / 2x the input rate."""
    assert cost_usd("claude-fable-5-1", cache_write_5m=1_000_000) == pytest.approx(12.50)
    assert cost_usd("claude-fable-5-1", cache_write_1h=1_000_000) == pytest.approx(20.00)


def test_opus_5_and_mythos_5_1_have_explicit_rows():
    """Previously they resolved only through the coarse family fallback; the
    prices are the family row's, so behaviour is unchanged but explicit."""
    assert "claude-opus-5" in pricing.RATES
    assert "claude-mythos-5-1" in pricing.RATES
    assert resolve_rate("claude-opus-5-20261001") is pricing.RATES["claude-opus-5"]
    rate = resolve_rate("claude-opus-5")
    assert (rate.input, rate.output) == (5.00, 25.00)
    # Same prefix-ordering rule as fable: 5-1 sits above 5, or a datestamped
    # 5-1 id would resolve through the older row.
    keys = list(pricing.RATES)
    assert keys.index("claude-mythos-5-1") < keys.index("claude-mythos-5")
    assert resolve_rate("claude-mythos-5-1-20261001") is pricing.RATES[
        "claude-mythos-5-1"]


def test_five_minute_cache_write_is_1_25x():
    assert cost_usd("claude-opus-4-8", cache_write_5m=1_000_000) == pytest.approx(6.25)


def test_one_hour_cache_write_is_2x():
    """The hour TTL costs double the input rate; claude-usage prices every write
    at the 5-minute rate and would understate an hour-cached workload by 37%."""
    assert cost_usd("claude-opus-4-8", cache_write_1h=1_000_000) == pytest.approx(10.00)


def test_components_sum():
    cost = cost_usd("claude-haiku-4-5", input_tokens=1_000_000,
                    output_tokens=1_000_000, cache_read=1_000_000,
                    cache_write_5m=1_000_000)
    assert cost == pytest.approx(1.00 + 5.00 + 0.10 + 1.25)


def test_negative_counts_cannot_create_a_credit():
    assert cost_usd("claude-opus-4-8", input_tokens=-1_000_000) == 0.0


def test_zero_usage_is_zero_not_none():
    assert cost_usd("claude-opus-4-8") == 0.0


def test_unknown_model_yields_none_not_zero():
    """A zero would sum into a total and quietly understate it, with no surface
    ever saying which turns were missing."""
    assert cost_usd("some-other-llm", input_tokens=1_000_000) is None


# --- promotional pricing -----------------------------------------------------


def test_sonnet_5_uses_the_introductory_rate_before_the_cutoff():
    """Sonnet 5 is $2/$10 through 2026-08-31 and $3/$15 after. Pricing a July
    session at the standard rate overstates it by a third."""
    cost = cost_usd("claude-sonnet-5", input_tokens=1_000_000,
                    output_tokens=1_000_000, day="2026-07-26")
    assert cost == pytest.approx(2.00 + 10.00)


def test_sonnet_5_uses_the_standard_rate_after_the_cutoff():
    cost = cost_usd("claude-sonnet-5", input_tokens=1_000_000,
                    output_tokens=1_000_000, day="2026-09-01")
    assert cost == pytest.approx(3.00 + 15.00)


def test_cutoff_day_is_inclusive():
    cost = cost_usd("claude-sonnet-5", input_tokens=1_000_000, day="2026-08-31")
    assert cost == pytest.approx(2.00)


def test_no_day_means_standard_rate():
    """Without a date we cannot know which rate applied; the standard one is the
    honest default rather than the cheaper promotional guess."""
    cost = cost_usd("claude-sonnet-5", input_tokens=1_000_000)
    assert cost == pytest.approx(3.00)


def test_cache_follows_the_promotional_input_rate():
    cost = cost_usd("claude-sonnet-5", cache_read=1_000_000, day="2026-07-26")
    assert cost == pytest.approx(0.20)      # 0.1 x the $2 intro rate


# --- overrides ---------------------------------------------------------------


def test_override_file_replaces_a_rate(tmp_path):
    pricing.PRICING_OVERRIDE_PATH.write_text(json.dumps(
        {"claude-opus-4-8": {"input": 7.5, "output": 30.0}}
    ))
    assert cost_usd("claude-opus-4-8", input_tokens=1_000_000) == pytest.approx(7.5)


def test_override_can_add_an_unknown_model():
    pricing.PRICING_OVERRIDE_PATH.write_text(json.dumps(
        {"claude-newthing-1": {"input": 4.0, "output": 20.0}}
    ))
    assert cost_usd("claude-newthing-1", output_tokens=1_000_000) == pytest.approx(20.0)


def test_malformed_entries_are_skipped_not_fatal():
    pricing.PRICING_OVERRIDE_PATH.write_text(json.dumps({
        "bad-1": {"input": 1.0},                    # missing output
        "bad-2": "nonsense",
        "bad-3": {"input": "free", "output": 1.0},  # unparseable
        "claude-opus-4-8": {"input": 9.0, "output": 9.0},
    }))
    assert cost_usd("claude-opus-4-8", input_tokens=1_000_000) == pytest.approx(9.0)
    assert resolve_rate("bad-1") is None


def test_string_intro_rates_are_coerced_to_float():
    """pricing.json is written by hand; "2.0" must not survive into for_day(),
    where int * str is string repetition, not arithmetic."""
    pricing.PRICING_OVERRIDE_PATH.write_text(json.dumps(
        {"claude-test-promo": {"input": 3.0, "output": 15.0,
                               "intro_input": "2.0", "intro_output": "10.0",
                               "intro_until": "2099-01-01"}}
    ))
    cost = cost_usd("claude-test-promo", input_tokens=1_000_000,
                    output_tokens=1_000_000, day="2026-09-01")
    assert cost == pytest.approx(2.00 + 10.00)


def test_unparseable_string_intro_rates_skip_the_entry():
    pricing.PRICING_OVERRIDE_PATH.write_text(json.dumps(
        {"claude-test-promo": {"input": 3.0, "output": 15.0,
                               "intro_input": "cheap"}}
    ))
    assert resolve_rate("claude-test-promo") is None


def test_unreadable_override_falls_back_to_builtins():
    pricing.PRICING_OVERRIDE_PATH.write_text("{not json")
    assert cost_usd("claude-opus-4-8", input_tokens=1_000_000) == pytest.approx(5.0)


def test_override_is_read_once(monkeypatch):
    pricing.PRICING_OVERRIDE_PATH.write_text(json.dumps(
        {"claude-opus-4-8": {"input": 1.0, "output": 1.0}}
    ))
    assert cost_usd("claude-opus-4-8", input_tokens=1_000_000) == pytest.approx(1.0)

    pricing.PRICING_OVERRIDE_PATH.write_text(json.dumps(
        {"claude-opus-4-8": {"input": 2.0, "output": 2.0}}
    ))
    assert cost_usd("claude-opus-4-8", input_tokens=1_000_000) == pytest.approx(1.0)


def test_rate_for_day_without_promo_ignores_the_date():
    rate = Rate(5.0, 25.0)
    assert rate.for_day("1999-01-01") == (5.0, 25.0)


def test_opus_5_5_has_its_own_row_above_opus_5():
    """Prefix matching takes the first hit, so without its own row above
    "claude-opus-5" an Opus 5.5 turn would be priced as Opus 5."""
    keys = list(pricing.RATES)
    assert keys.index("claude-opus-5-5") < keys.index("claude-opus-5")
    rate = resolve_rate("claude-opus-5-5")
    assert (rate.input, rate.output) == (4.00, 20.00)
    assert resolve_rate("claude-opus-5-5[1m]") is rate
    assert cost_usd("claude-opus-5-5", cache_read=1_000_000) == pytest.approx(0.20)
