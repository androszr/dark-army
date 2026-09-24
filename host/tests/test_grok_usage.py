"""Grok updates.jsonl: ticks, incremental cache, truncated tail."""
import json
import os

from dark_army_daemon.grok_usage import (
    USD_TICKS,
    UsageCache,
    parse_turn_record,
    parse_updates,
)


def _turn(ticks, input_tokens=100, output_tokens=10, model_calls=1,
          models=None, timestamp=1_786_000_000, **flags):
    usage = {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "cachedReadTokens": 0,
        "cacheCreationTokens": 0,
        "reasoningTokens": 0,
        "modelCalls": model_calls,
        "numTurns": model_calls,
        "apiDurationMs": 100,
        "modelUsage": models if models is not None else {"grok-4.6-build": {}},
    }
    if ticks is not None:
        usage["costUsdTicks"] = ticks
    usage.update(flags)
    return {
        "method": "_x.ai/session/update",
        "timestamp": timestamp,
        "params": {"update": {
            "sessionUpdate": "turn_completed",
            "stop_reason": "end_turn",
            "usage": usage,
        }},
    }


def _write(path, turns):
    path.write_text("".join(json.dumps(t) + "\n" for t in turns), encoding="utf-8")
    return str(path)


def test_parse_turn_record_strips_build_suffix_and_converts_ticks():
    rec = _turn(USD_TICKS, timestamp=50)
    rec["params"]["sessionId"] = "sid-9"
    turn = parse_turn_record(rec)
    assert turn["cost_usd"] == 1.0
    assert turn["model"] == "grok-4.6"
    assert turn["message_id"] == "grok:sid-9:50"
    assert turn["provider"] == "grok"


def test_parse_turn_record_omits_cost_when_unpriced():
    turn = parse_turn_record(_turn(None, timestamp=50))
    assert turn["cost_usd"] is None


def test_ticks_become_dollars_at_1e10():
    assert USD_TICKS == 10_000_000_000
    assert 1_529_758_600 / USD_TICKS == 0.15297586


def test_a_multi_turn_session_sums_ticks_not_the_last(tmp_path):
    # The live record the plan quoted: $0.15297586 on one turn, plus a second.
    path = _write(tmp_path / "updates.jsonl", [
        _turn(1_529_758_600, input_tokens=928_960, output_tokens=32_291,
              model_calls=14, timestamp=100),
        _turn(2 * USD_TICKS, input_tokens=50, output_tokens=5,
              model_calls=1, timestamp=200),
    ])
    usage = parse_updates(path)
    assert usage.turns == 2
    assert usage.cost_usd == (1_529_758_600 + 2 * USD_TICKS) / USD_TICKS
    assert usage.cost_is_partial is False
    assert usage.input_tokens == 928_960 + 50
    assert usage.output_tokens == 32_291 + 5
    assert usage.model_calls == 15
    assert usage.models == ["grok-4.6-build"]
    assert usage.last_input_tokens == 50
    assert usage.last_prompt_tokens == 50


def test_a_turn_without_ticks_poisons_the_total(tmp_path):
    path = _write(tmp_path / "updates.jsonl", [
        _turn(USD_TICKS),
        _turn(None),
    ])
    usage = parse_updates(path)
    assert usage.cost_usd is None
    assert usage.cost_is_partial is True
    assert usage.turns == 2
    assert usage.input_tokens == 200          # tokens still roll up


def test_incomplete_flag_poisons_even_when_ticks_are_present(tmp_path):
    path = _write(tmp_path / "updates.jsonl", [
        _turn(USD_TICKS, cost_is_partial=True),
    ])
    usage = parse_updates(path)
    assert usage.cost_usd is None
    assert usage.cost_is_partial is True


def test_zero_ticks_is_a_real_free_turn_not_unknown(tmp_path):
    path = _write(tmp_path / "updates.jsonl", [_turn(0)])
    usage = parse_updates(path)
    assert usage.cost_usd == 0.0
    assert usage.cost_is_partial is False


def test_no_usage_records_is_unknown_not_partial(tmp_path):
    path = tmp_path / "updates.jsonl"
    path.write_text(json.dumps({"sessionUpdate": "tool_call"}) + "\n")
    usage = parse_updates(str(path))
    assert usage.turns == 0
    assert usage.cost_usd is None
    assert usage.cost_is_partial is False


def test_missing_file_is_empty():
    usage = parse_updates("/no/such/updates.jsonl")
    assert usage.turns == 0
    assert usage.cost_usd is None


def test_incremental_read_only_parses_the_delta(tmp_path):
    path = tmp_path / "updates.jsonl"
    path.write_text(json.dumps(_turn(USD_TICKS)) + "\n")
    os.utime(path, (1, 1))
    cache = UsageCache()
    first = cache.get(str(path))
    assert first.turns == 1
    assert first.cost_usd == 1.0
    assert cache.last_folded == 1

    # Same mtime: nothing to do.
    again = cache.get(str(path))
    assert again.turns == 1
    assert cache.last_folded == 0

    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_turn(2 * USD_TICKS, input_tokens=40)) + "\n")
    os.utime(path, (2, 2))
    second = cache.get(str(path))
    assert cache.last_folded == 1
    assert second.turns == 2
    assert second.cost_usd == 3.0
    assert second.input_tokens == 140


def test_append_without_mtime_change_is_still_read(tmp_path):
    path = tmp_path / "updates.jsonl"
    path.write_text(json.dumps(_turn(USD_TICKS)) + "\n")
    os.utime(path, (1, 1))
    cache = UsageCache()
    assert cache.get(str(path)).turns == 1
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_turn(2 * USD_TICKS, input_tokens=40)) + "\n")
    os.utime(path, (1, 1))
    second = cache.get(str(path))
    assert cache.last_folded == 1
    assert second.turns == 2
    assert second.cost_usd == 3.0


def test_truncated_last_line_is_held_until_it_completes(tmp_path):
    first = json.dumps(_turn(USD_TICKS)) + "\n"
    second = json.dumps(_turn(2 * USD_TICKS)) + "\n"
    path = tmp_path / "updates.jsonl"
    path.write_bytes((first + second[:40]).encode())
    os.utime(path, (1, 1))
    cache = UsageCache()
    assert cache.get(str(path)).turns == 1

    path.write_bytes((first + second).encode())
    os.utime(path, (2, 2))
    got = cache.get(str(path))
    assert got.turns == 2
    assert cache.last_folded == 1
    assert got.cost_usd == 3.0


def test_a_line_without_usage_is_not_json_parsed_as_a_turn(tmp_path):
    path = tmp_path / "updates.jsonl"
    path.write_text(
        json.dumps({"params": {"update": {"sessionUpdate": "tool_call"}}}) + "\n"
        + json.dumps(_turn(USD_TICKS)) + "\n"
    )
    usage = parse_updates(str(path))
    assert usage.turns == 1


def test_usage_found_under_meta_not_just_params_update(tmp_path):
    record = {
        "timestamp": 1,
        "_meta": {"usage": {
            "inputTokens": 10, "outputTokens": 2, "costUsdTicks": USD_TICKS,
            "modelCalls": 1, "modelUsage": {"grok-4.6-build": {}},
        }},
    }
    path = tmp_path / "updates.jsonl"
    path.write_text(json.dumps(record) + "\n")
    usage = parse_updates(str(path))
    assert usage.turns == 1
    assert usage.cost_usd == 1.0
    assert usage.models == ["grok-4.6-build"]


def test_last_prompt_tokens_divides_a_multi_call_turn(tmp_path):
    path = _write(tmp_path / "updates.jsonl", [
        _turn(USD_TICKS, input_tokens=928_960, model_calls=14),
    ])
    usage = parse_updates(path)
    assert usage.last_prompt_tokens == 928_960 // 14


def _live(total_tokens, timestamp=1_786_000_000):
    """A mid-turn tool_call_update with Grok's live context stamp."""
    return {
        "timestamp": timestamp,
        "method": "session/update",
        "params": {
            "_meta": {"totalTokens": total_tokens},
            "update": {"sessionUpdate": "tool_call_update"},
        },
    }


def test_meta_total_tokens_is_the_live_context_fill(tmp_path):
    path = _write(tmp_path / "updates.jsonl", [_live(98_853)])
    usage = parse_updates(path)
    assert usage.last_total_tokens == 98_853
    assert usage.turns == 0
    assert usage.cost_usd is None
    assert usage.last_prompt_tokens == 0


def test_usage_total_tokens_is_not_the_live_fill(tmp_path):
    """usage.totalTokens is the sum of every call in the turn, often > window."""
    rec = _turn(USD_TICKS, input_tokens=928_960, model_calls=14)
    rec["params"]["update"]["usage"]["totalTokens"] = 1_087_777
    path = _write(tmp_path / "updates.jsonl", [rec])
    usage = parse_updates(path)
    assert usage.last_total_tokens == 0
    assert usage.last_prompt_tokens == 928_960 // 14
    assert usage.turns == 1


def test_live_tokens_update_incrementally(tmp_path):
    path = tmp_path / "updates.jsonl"
    path.write_text(json.dumps(_live(21_130)) + "\n")
    os.utime(path, (1, 1))
    cache = UsageCache()
    first = cache.get(str(path))
    assert first.last_total_tokens == 21_130
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_live(98_853)) + "\n")
    os.utime(path, (2, 2))
    second = cache.get(str(path))
    assert second.last_total_tokens == 98_853
    assert second.turns == 0


# --- the shared incremental reader (DeltaCache) --------------------------------
#
# UsageCache / EventsCache / ChatCache used to be three line-for-line copies of
# one loop; DeltaCache is that loop, once. Pinned directly so a regression in
# the shared machinery is named here rather than three files away.


def test_delta_cache_is_the_one_loop_behind_all_three_readers():
    from dark_army_daemon.grok_chat import ChatCache
    from dark_army_daemon.grok_events import EventsCache
    from dark_army_daemon.grok_usage import DeltaCache

    assert issubclass(UsageCache, DeltaCache)
    assert issubclass(EventsCache, DeltaCache)
    assert issubclass(ChatCache, DeltaCache)


def test_delta_cache_folds_only_the_appended_bytes(tmp_path):
    from dataclasses import dataclass, field

    from dark_army_daemon.grok_usage import DeltaCache

    @dataclass
    class _Acc:
        lines: list = field(default_factory=list)
        offset: int = 0
        folded: int = 0

    def _fold(line, acc):
        acc.folded += 1
        acc.lines.append(line)

    cache = DeltaCache(_Acc, _fold, lambda acc: list(acc.lines))
    path = tmp_path / "log.jsonl"
    path.write_text("a\n")
    os.utime(path, (1, 1))
    assert cache.get(str(path)) == ["a"]
    assert cache.last_folded == 1

    # Unchanged mtime: memoised, nothing folded.
    assert cache.get(str(path)) == ["a"]
    assert cache.last_folded == 0

    # Grown: only the delta is folded.
    path.write_text("a\nb\n")
    os.utime(path, (2, 2))
    assert cache.get(str(path)) == ["a", "b"]
    assert cache.last_folded == 1

    # Shrunk below the offset: rebuilt whole, not resumed mid-nothing.
    path.write_text("c\n")
    os.utime(path, (3, 3))
    assert cache.get(str(path)) == ["c"]

    # Missing path / missing file: the empty result, not an exception.
    assert cache.get("") == []
    assert cache.get(str(tmp_path / "gone.jsonl")) == []


def test_iso_epoch_is_the_one_parser_for_grok_timestamps():
    """grok_billing, grok_events and grok_roster each grew a private copy of
    this; they now all route here."""
    from dark_army_daemon import grok_billing, grok_roster
    from dark_army_daemon.grok_events import _ts_of
    from dark_army_daemon.grok_usage import iso_epoch

    assert iso_epoch(1_786_000_000) == 1_786_000_000.0
    assert iso_epoch("1970-01-01T00:00:10+00:00") == 10.0
    assert iso_epoch("2026-08-30T12:00:00Z") is not None
    assert iso_epoch(True) is None          # bool is not a timestamp
    assert iso_epoch("not a date") is None
    assert iso_epoch(None) is None
    assert iso_epoch("") is None

    assert grok_roster._epoch is iso_epoch
    assert grok_billing._iso_epoch is iso_epoch
    assert _ts_of({"ts": "1970-01-01T00:00:10+00:00"}) == 10.0
