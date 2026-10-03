"""Run health on the card: the pure rules, the ledger, the three loop-side
counters, the store's two counts, the decoration, the freeze, the marker
and the source pins (`plans/2026-09-13-run-health-on-card.md`).

Every figure on the line is decided in `run_health.py` off data the daemon
already has and drawn verbatim by both clients; the tests here pin the
decision, and the Swift tests pin the words.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import pytest

from dark_army_daemon import channel_server as cs
from dark_army_daemon import daemon as daemon_mod
from dark_army_daemon import run_health as rh
from dark_army_daemon import session_store
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon
from dark_army_daemon.session_stats import (
    AgentInfo, MAX_SPAWN_TYPES, SessionStats, stats_to_dict,
)

REPO = Path(__file__).resolve().parents[2]
DEFAULT_CUTS = ("default", rh.DEFAULT_TYPICAL, rh.DEFAULT_LARGE)


# ── the figures ──────────────────────────────────────────────────────────────

def test_turns_are_exact_to_twenty_then_floored_to_five():
    assert rh.quantise_turns(17) == 17
    assert rh.quantise_turns(20) == 20
    assert rh.quantise_turns(23) == 20
    assert rh.quantise_turns(84) == 80
    assert rh.quantise_turns(None) is None


def test_tokens_are_thousands_to_two_significant_figures():
    assert rh.quantise_tokens_k(999_499) == 990
    assert rh.quantise_tokens_k(2_140_000) == 2100
    assert rh.quantise_tokens_k(12_345) == 12
    assert rh.quantise_tokens_k(4_200) == 4
    assert rh.quantise_tokens_k(None) is None


def test_context_is_floored_to_five():
    assert rh.quantise_ctx(67) == 65
    assert rh.quantise_ctx(90) == 90
    assert rh.quantise_ctx(103) == 100
    assert rh.quantise_ctx("42") == 40
    assert rh.quantise_ctx(None) is None


def test_tokens_of_is_fresh_input_plus_output_never_cache_reads():
    stats = {"total_input_tokens": 1_000, "output_tokens": 250,
             "cache_read_tokens": 9_000_000}
    assert rh.tokens_of(stats) == 1_250
    assert rh.tokens_of({}) is None
    assert rh.tokens_of(None) is None


# ── the yardstick ────────────────────────────────────────────────────────────

def _record(root, turns, tokens_k, at=1_000.0):
    return {"card_id": f"c-{turns}-{tokens_k}", "root": root, "at": at,
            "dispatched_at": at, "reading": {"turns": turns, "tokens_k": tokens_k}}


def test_baseline_is_the_defaults_under_five_runs():
    records = [_record("/p", 10, 100) for _ in range(4)]
    assert rh.baseline(records, "/p") == DEFAULT_CUTS
    assert rh.baseline([], "/p") == DEFAULT_CUTS


def test_baseline_is_the_projects_own_medians_at_five():
    records = [_record("/p", t, k) for t, k in
               [(10, 100), (20, 200), (30, 300), (40, 400), (50, 500)]]
    basis, typical, large = rh.baseline(records, "/p")
    assert basis == "project"
    assert typical == (45, 450_000)
    assert large == (90, 900_000)


def test_baseline_ignores_another_roots_runs():
    records = [_record("/other", 10, 100) for _ in range(5)]
    assert rh.baseline(records, "/p") == DEFAULT_CUTS
    records.append(_record("/p", 10, 100))
    assert rh.baseline(records, "/p") == DEFAULT_CUTS


def test_classify_on_turns_alone_tokens_alone_and_both():
    cuts = ((40, 1_500_000), (120, 5_000_000))
    assert rh.classify(10, 100, cuts) == "typical"
    assert rh.classify(41, 100, cuts) == "large"
    assert rh.classify(121, 100, cuts) == "worrying"
    assert rh.classify(10, 1_500_001, cuts) == "large"
    assert rh.classify(10, 5_000_001, cuts) == "worrying"
    assert rh.classify(121, 5_000_001, cuts) == "worrying"
    assert rh.classify(10, None, cuts) == "typical"


def test_classify_is_empty_without_turns():
    assert rh.classify(None, 9_000_000, DEFAULT_CUTS[1:]) == ""


def test_fix_rounds_are_implementer_spawns_after_the_first():
    assert rh.fix_rounds({"bc-implementer": 3}) == 2
    assert rh.fix_rounds({"bc-implementer": 1, "bc-verifier": 4}) == 0
    assert rh.fix_rounds({}) == 0
    assert rh.fix_rounds(None) is None


def test_attention_on_worrying_full_context_or_three_fix_rounds():
    assert rh.attention("worrying", None, None)
    assert rh.attention("typical", 90, None)
    assert rh.attention("typical", 85, None)
    assert rh.attention("typical", None, 3)
    assert not rh.attention("typical", 80, 2)
    assert not rh.attention("large", None, None)
    assert not rh.attention("", None, None)


# ── compose ──────────────────────────────────────────────────────────────────

def _row(**over):
    row = {
        "session_id": "s1", "provider": "claude",
        "stats": {"assistant_messages": 84, "total_input_tokens": 2_000_000,
                  "output_tokens": 140_000, "tool_counts": {"AskUserQuestion": 2},
                  "spawn_counts": {"bc-implementer": 3, "bc-verifier": 1}},
        "metrics": {"ctx_used_pct": 67},
        "permission_asks": 1, "permission_denied": 1, "stop_failures": 0,
    }
    row.update(over)
    return row


def test_compose_a_full_row_is_the_exact_dict():
    out = rh.compose(_row(), {"attempts": 2, "returns": 1}, None, DEFAULT_CUTS)
    assert out == {
        "class": "large", "basis": "default", "turns": 80, "tokens_k": 2100,
        "ctx_pct": 65, "asks": 3, "refusals": 1, "attempts": 2, "returns": 1,
        "fix_rounds": 2, "attention": False, "live": True, "effort": "",
    }


def test_compose_key_set_is_pinned_exactly():
    out = rh.compose(_row(), {"attempts": 1, "returns": 0}, None, DEFAULT_CUTS)
    assert set(out) == rh.PUBLISHED_KEYS
    for word in ("cost", "duration", "usd", "seconds"):
        assert not any(word in k for k in out)


def test_compose_without_metrics_omits_the_context():
    out = rh.compose(_row(metrics={}), {}, None, DEFAULT_CUTS)
    assert "ctx_pct" not in out
    out = rh.compose(_row(metrics=None), {}, None, DEFAULT_CUTS)
    assert "ctx_pct" not in out


def test_compose_a_codex_row_omits_fix_rounds():
    row = _row(provider="codex")
    row["stats"]["spawn_counts"] = {}
    out = rh.compose(row, {}, None, DEFAULT_CUTS)
    assert "fix_rounds" not in out
    # And so does a row whose stats predate the key.
    row = _row()
    del row["stats"]["spawn_counts"]
    assert "fix_rounds" not in rh.compose(row, {}, None, DEFAULT_CUTS)


def test_compose_a_grok_row_omits_fix_rounds():
    """Grok keeps no transcript-backed spawn list either: its row publishes
    `spawn_counts: {}` whatever ran, and `fixes 0` would be a count nobody
    took. Withheld for every `NO_SPAWN_LIST_PROVIDERS` member; a Claude row
    with the same empty dict still reads 0, because Claude's list is real."""
    row = _row(provider="grok")
    row["stats"]["spawn_counts"] = {}
    assert "fix_rounds" not in rh.compose(row, {}, None, DEFAULT_CUTS)
    row = _row(provider="grok")
    assert "fix_rounds" not in rh.compose(row, {}, None, DEFAULT_CUTS)
    assert rh.NO_SPAWN_LIST_PROVIDERS == {"codex", "grok"}
    row = _row(provider="claude")
    row["stats"]["spawn_counts"] = {}
    assert rh.compose(row, {}, None, DEFAULT_CUTS)["fix_rounds"] == 0


def test_compose_a_row_without_stats_is_unsized():
    out = rh.compose(_row(stats={}), {}, None, DEFAULT_CUTS)
    assert out["class"] == ""
    assert "turns" not in out and "tokens_k" not in out
    assert out["asks"] == 1 and out["refusals"] == 1


def test_compose_reads_the_frozen_entry_when_there_is_no_row():
    entry = {"card_id": "c1", "root": "/p", "at": 1.0, "dispatched_at": 1.0,
             "reading": {"class": "typical", "basis": "project", "turns": 15,
                         "tokens_k": 300, "asks": 0, "refusals": 0,
                         "attempts": 1, "returns": 0, "fix_rounds": 0,
                         "attention": False, "live": True}}
    out = rh.compose(None, {"attempts": 1, "returns": 1}, entry, DEFAULT_CUTS)
    assert out["live"] is False
    assert out["turns"] == 15 and out["tokens_k"] == 300
    assert out["class"] == "typical" and out["basis"] == "project"
    # The store's current counts win: a card sent back after its run
    # froze still shows the return.
    assert out["returns"] == 1


def test_compose_is_none_with_neither_row_nor_entry():
    assert rh.compose(None, {"attempts": 3, "returns": 0}, None, DEFAULT_CUTS) is None


def test_compose_attention_follows_the_three_rules():
    row = _row(metrics={"ctx_used_pct": 91})
    assert rh.compose(row, {}, None, DEFAULT_CUTS)["attention"] is True
    row = _row()
    row["stats"]["spawn_counts"] = {"bc-implementer": 4}
    assert rh.compose(row, {}, None, DEFAULT_CUTS)["attention"] is True
    row = _row()
    row["stats"]["assistant_messages"] = 500
    out = rh.compose(row, {}, None, DEFAULT_CUTS)
    assert out["class"] == "worrying" and out["attention"] is True


def test_two_rows_one_turn_apart_compose_equal_above_twenty():
    """The churn pin: a per-turn figure would make the board news on every
    assistant message of every bound session."""
    a = _row()
    a["stats"]["assistant_messages"] = 82
    b = _row()
    b["stats"]["assistant_messages"] = 83
    b["stats"]["output_tokens"] = a["stats"]["output_tokens"] + 900
    b["metrics"]["ctx_used_pct"] = 68
    assert rh.compose(a, {}, None, DEFAULT_CUTS) == rh.compose(b, {}, None, DEFAULT_CUTS)


# ── the ledger ───────────────────────────────────────────────────────────────

@pytest.fixture
def ledger_path(tmp_path):
    return tmp_path / "run-health.json"


def _reading(**over):
    base = {"class": "typical", "basis": "default", "turns": 12, "tokens_k": 200,
            "asks": 0, "refusals": 0, "attempts": 1, "returns": 0,
            "fix_rounds": 0, "attention": False, "live": True}
    base.update(over)
    return base


def test_ledger_freezes_and_reloads(ledger_path):
    ledger = rh.Ledger.load(ledger_path)
    assert ledger.entries == []
    assert ledger.freeze("c1", "/p", _reading(), 100.0, now=1_000.0)
    again = rh.Ledger.load(ledger_path)
    entry = again.entry_for("c1")
    assert entry["root"] == "/p" and entry["dispatched_at"] == 100.0
    assert entry["reading"]["turns"] == 12
    assert entry["reading"]["live"] is False
    assert again.for_root("/p") == [entry]
    assert again.for_root("/other") == []
    # And the file is the private ring's shape.
    assert oct(ledger_path.stat().st_mode & 0o777) == "0o600"


def test_ledger_freeze_is_idempotent_on_the_reading_and_replaces_on_a_new_dispatch(
        ledger_path):
    ledger = rh.Ledger.load(ledger_path)
    assert ledger.freeze("c1", "/p", _reading(turns=5), 100.0, now=1_000.0)
    # The identical reading for the same dispatch is one entry, unwritten.
    mtime = ledger_path.stat().st_mtime_ns
    assert not ledger.freeze("c1", "/p", _reading(turns=5), 100.0, now=1_001.0)
    assert ledger_path.stat().st_mtime_ns == mtime
    assert len(rh.Ledger.load(ledger_path).entries) == 1
    # A later dispatch of the same card replaces the entry: one per card.
    assert ledger.freeze("c1", "/p", _reading(turns=9), 200.0, now=1_002.0)
    loaded = rh.Ledger.load(ledger_path)
    assert len(loaded.entries) == 1
    assert loaded.entry_for("c1")["reading"]["turns"] == 9


def test_ledger_a_newer_reading_for_the_same_dispatch_overwrites(ledger_path):
    """An `ended → live` resume keeps the card's `dispatched_at`, so the
    second quiet spell freezes under the same key: a reading with more turns
    replaces the first; one that went backwards (not the same transcript)
    is left alone; the same reading again stays one entry."""
    ledger = rh.Ledger.load(ledger_path)
    assert ledger.freeze("c1", "/p", _reading(turns=5, tokens_k=100), 100.0,
                         now=1_000.0)
    assert ledger.freeze("c1", "/p", _reading(turns=9, tokens_k=180), 100.0,
                         now=1_001.0)
    loaded = rh.Ledger.load(ledger_path)
    assert len(loaded.entries) == 1
    assert loaded.entry_for("c1")["reading"]["turns"] == 9
    assert loaded.entry_for("c1")["dispatched_at"] == 100.0
    # Same turns, more tokens: newer.
    assert ledger.freeze("c1", "/p", _reading(turns=9, tokens_k=200), 100.0,
                         now=1_002.0)
    assert rh.Ledger.load(ledger_path).entry_for("c1")["reading"]["tokens_k"] == 200
    # Fewer turns: not this run's later reading.
    assert not ledger.freeze("c1", "/p", _reading(turns=3, tokens_k=300), 100.0,
                             now=1_003.0)
    assert rh.Ledger.load(ledger_path).entry_for("c1")["reading"]["turns"] == 9
    # And the identical reading writes nothing.
    assert not ledger.freeze("c1", "/p", _reading(turns=9, tokens_k=200), 100.0,
                             now=1_004.0)
    assert len(rh.Ledger.load(ledger_path).entries) == 1


def test_ledger_is_bounded_at_max_and_ages_out(ledger_path):
    # A real clock: `load()` prunes on the wall clock too, so a 1970 stamp
    # would age every entry out before the bound is even asked.
    now = time.time()
    entries = [{"card_id": f"c{i}", "root": "/p", "at": now - i,
                "dispatched_at": 1.0, "reading": _reading()}
               for i in range(rh.MAX_LEDGER + 20)]
    entries.append({"card_id": "old", "root": "/p",
                    "at": now - (rh.LEDGER_DAYS + 1) * 86400,
                    "dispatched_at": 1.0, "reading": _reading()})
    ledger_path.write_text(json.dumps({"version": 1, "entries": entries}))
    ledger = rh.Ledger.load(ledger_path)
    ledger._prune(now)
    assert len(ledger.entries) == rh.MAX_LEDGER
    assert ledger.entry_for("old") is None
    # Newest kept: the oldest of the in-window entries fell off.
    assert ledger.entry_for("c0") is not None
    assert ledger.entry_for(f"c{rh.MAX_LEDGER + 19}") is None


def test_ledger_corrupt_file_reads_empty(ledger_path):
    ledger_path.write_text("{not json")
    assert rh.Ledger.load(ledger_path).entries == []
    ledger_path.write_text(json.dumps([1, 2, 3]))
    assert rh.Ledger.load(ledger_path).entries == []


def test_ledger_well_formed_entry_with_a_bad_field_reads_as_absent(ledger_path):
    """Valid JSON with a string where a number goes must not raise: the
    load sits bare in `_build_board_state`, and a raise there would take
    the whole board down for the daemon's life."""
    now = time.time()
    good = {"card_id": "ok", "root": "/p", "at": now, "dispatched_at": 1.0,
            "reading": {"class": "large", "turns": "84", "asks": [1],
                        "attention": 1, "ctx_pct": {"x": 1}}}
    ledger_path.write_text(json.dumps({"version": 1, "entries": [
        {"card_id": "c1", "root": "/p", "at": "abc", "dispatched_at": 1.0,
         "reading": {"class": "large"}},
        {"card_id": "c2", "root": "/p", "at": now, "dispatched_at": [1],
         "reading": {"class": "large"}},
        {"card_id": "c3", "root": "/p", "at": now, "dispatched_at": 1.0,
         "reading": "not a dict"},
        {"card_id": 7, "root": "/p", "at": now, "dispatched_at": 1.0,
         "reading": {}},
        "garbage", None, good,
    ]}))
    ledger = rh.Ledger.load(ledger_path)
    assert [e["card_id"] for e in ledger.entries] == ["ok"]
    reading = ledger.entry_for("ok")["reading"]
    # Coercible values are re-typed, uncoercible ones dropped.
    assert reading == {"class": "large", "turns": 84, "attention": True}
    published = rh.compose(None, {"attempts": 1, "returns": 0},
                           ledger.entry_for("ok"), None)
    assert published["turns"] == 84 and published["live"] is False
    assert "asks" in published and published["asks"] == 0


def test_ledger_load_is_memoised_on_the_file(ledger_path):
    first = rh.Ledger.load(ledger_path)
    assert rh.Ledger.load(ledger_path) is first
    first.freeze("c1", "/p", _reading(), 1.0)
    assert rh.Ledger.load(ledger_path) is first
    # A write behind its back is seen.
    ledger_path.write_text(json.dumps({"version": 1, "entries": []}))
    assert rh.Ledger.load(ledger_path) is not first


def test_ledger_cuts_for_a_root_become_the_project_at_five(ledger_path):
    ledger = rh.Ledger.load(ledger_path)
    for i in range(5):
        ledger.freeze(f"c{i}", "/p", _reading(turns=10 * (i + 1),
                                                tokens_k=100 * (i + 1)),
                      float(i + 1), now=1_000.0 + i)
    basis, typical, large = ledger.cuts_for("/p")
    assert basis == "project"
    assert typical == (45, 450_000)
    assert ledger.cuts_for("/other") == DEFAULT_CUTS


# ── spawn counts ─────────────────────────────────────────────────────────────

def test_stats_to_dict_counts_one_per_spawn_and_is_bounded():
    s = SessionStats()
    for i in range(3):
        s.agents[f"a{i}"] = AgentInfo(agent_id=f"a{i}", subagent_type="bc-implementer")
    s.agents["v"] = AgentInfo(agent_id="v", subagent_type="bc-verifier")
    s.agents["blank"] = AgentInfo(agent_id="blank", subagent_type="")
    assert stats_to_dict(s)["spawn_counts"] == {"bc-implementer": 3, "bc-verifier": 1}
    for i in range(MAX_SPAWN_TYPES + 5):
        s.agents[f"t{i}"] = AgentInfo(agent_id=f"t{i}", subagent_type=f"type-{i}")
    counts = stats_to_dict(s)["spawn_counts"]
    assert len(counts) == MAX_SPAWN_TYPES
    assert counts["bc-implementer"] == 3  # the most frequent are kept


def test_stats_to_dict_of_an_empty_transcript_has_an_empty_spawn_dict():
    assert stats_to_dict(SessionStats())["spawn_counts"] == {}


# ── the counters ─────────────────────────────────────────────────────────────

def _daemon(tmp_path):
    return BobDaemon(headless=True, sessions_path=tmp_path / "sessions.json")


def _known(daemon, sid="s1"):
    daemon._session_states[sid] = {
        "state": "working", "last_event": time.time(), "pid": 4242,
    }
    return sid


def _ask(daemon, request_id="hook-1"):
    return asyncio.run(daemon._handle_message({
        "event": "permission_ask", "session_id": "s1", "cwd": "/x/proj",
        "request_id": request_id, "claim": "cl41m", "tool_name": "Read",
        "description": "/etc/hosts", "input_preview": "{}",
    }))


def test_a_hook_permission_ask_counts_once(tmp_path):
    d = _daemon(tmp_path)
    _known(d)
    assert _ask(d)["hold"] > 0
    assert d._session_states["s1"]["permission_asks"] == 1
    assert d._session_states["s1"].get("permission_denied", 0) == 0


def test_a_deny_counts_a_refusal_and_an_allow_does_not(tmp_path):
    d = _daemon(tmp_path)
    _known(d)
    _ask(d, "r1")
    ok, _ = asyncio.run(d.answer_permission("r1", "deny"))
    assert ok
    assert d._session_states["s1"]["permission_denied"] == 1
    _ask(d, "r2")
    ok, _ = asyncio.run(d.answer_permission("r2", "allow"))
    assert ok
    assert d._session_states["s1"]["permission_denied"] == 1
    assert d._session_states["s1"]["permission_asks"] == 2


def _channel_ask(daemon, request_id="ch-1", port=51000):
    daemon._handle_channel_message({
        "type": "channel_attach", "port": port, "pid": 4242, "cwd": "/tmp",
        "session_id": "", "is_channel": True, "secret": "",
        "host": cs.HOST_CLAUDE})
    daemon._handle_channel_message({
        "type": "channel_permission_request", "port": port, "pid": 4242,
        "request_id": request_id, "tool_name": "Bash",
        "description": "rm -rf .build", "input_preview": "{}"})


def test_a_channel_deny_counts_only_where_the_push_landed(tmp_path):
    """The verdict is counted just before each successful `return True`,
    never before the channel has taken it: a refusal nobody received is not
    a refusal the run hit."""
    d = _daemon(tmp_path)
    _known(d)
    _channel_ask(d, "ch-1")
    assert d._session_states["s1"]["permission_asks"] == 1

    async def refused(port, payload):
        return False

    d._send_to_channel = refused
    ok, detail = asyncio.run(d.answer_permission("ch-1", "deny"))
    assert not ok and "no longer listening" in detail
    assert d._session_states["s1"].get("permission_denied", 0) == 0

    async def landed(port, payload):
        return True

    _channel_ask(d, "ch-2")
    d._send_to_channel = landed
    ok, _ = asyncio.run(d.answer_permission("ch-2", "deny"))
    assert ok
    assert d._session_states["s1"]["permission_denied"] == 1
    # An allow that lands steps nothing.
    _channel_ask(d, "ch-3")
    ok, _ = asyncio.run(d.answer_permission("ch-3", "allow"))
    assert ok
    assert d._session_states["s1"]["permission_denied"] == 1


def test_a_hook_deny_on_a_lapsed_poll_counts_nothing(tmp_path):
    d = _daemon(tmp_path)
    _known(d)
    _ask(d, "r1")
    d._permission_requests["r1"]["last_poll_at"] = (
        time.time() - daemon_mod.HOOK_PROMPT_POLL_LAPSE_SECONDS - 1)
    ok, detail = asyncio.run(d.answer_permission("r1", "deny"))
    assert not ok and "no longer listening" in detail
    assert d._session_states["s1"].get("permission_denied", 0) == 0


def test_the_deny_count_sits_before_each_landed_return_only():
    """Source pin: `answer_permission` steps the counter exactly three times
    (grok key press, hook, channel), and each site is followed by its
    `return True, ""`, with at most a display push between."""
    src = (REPO / "host" / "dark_army_daemon" / "daemon.py").read_text()
    start = src.index("    async def answer_permission(")
    end = src.index("\n    def ", start + 10)
    body = src[start:end]
    sites = [m.start() for m in re.finditer(
        r'self\._count_on_session\(sid, "permission_denied"\)', body)]
    assert len(sites) == 3
    for at in sites:
        following = [line.strip() for line in body[at:].split("\n", 3)[1:3]]
        if following[0] == "self._schedule_display_push()":
            following = following[1:]
        assert following[0] == 'return True, ""', following
    # And it is stepped nowhere above the first per-provider branch.
    head = body[:body.index('if request.get("via") == "codex-hook"')]
    assert "permission_denied\")" not in head


def test_a_stop_failure_counts(tmp_path):
    d = _daemon(tmp_path)
    asyncio.run(d._handle_message({
        "event": "add", "hook": "StopFailure", "session_id": "s1",
        "project": "p", "cwd": "/x/proj", "message": "API error"}))
    assert d._session_states["s1"]["stop_failures"] == 1
    asyncio.run(d._handle_message({
        "event": "add", "hook": "StopFailure", "session_id": "s1",
        "project": "p", "cwd": "/x/proj", "message": "API error"}))
    assert d._session_states["s1"]["stop_failures"] == 2


def test_the_counters_round_trip_sessions_json_and_default_to_zero(tmp_path):
    path = tmp_path / "sessions.json"
    session_store.save_sessions({
        "s1": {"state": "idle", "last_event": time.time(),
               "permission_asks": 2, "permission_denied": 1, "stop_failures": 3},
        "s2": {"state": "idle", "last_event": time.time()},
    }, path)
    loaded = session_store.load_sessions(path)
    assert loaded["s1"]["permission_asks"] == 2
    assert loaded["s1"]["permission_denied"] == 1
    assert loaded["s1"]["stop_failures"] == 3
    for key in rh.COUNTER_KEYS:
        assert loaded["s2"].get(key, 0) == 0


def test_the_stub_carries_the_counters_and_a_bare_state_reads_zero(tmp_path):
    d = _daemon(tmp_path)
    d._session_states["s1"] = {"state": "working", "last_event": time.time(),
                               "permission_asks": 4}
    d._session_states["s2"] = {"state": "working", "last_event": time.time()}
    stubs = {s["session_id"]: s for s in d._collect_agent_stubs()}
    assert stubs["s1"]["permission_asks"] == 4
    assert stubs["s1"]["permission_denied"] == 0
    assert stubs["s2"]["stop_failures"] == 0


def test_a_counter_never_creates_a_session(tmp_path):
    d = _daemon(tmp_path)
    d._count_on_session("ghost", "permission_asks")
    assert "ghost" not in d._session_states


# ── the store's two counts ───────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    s = BoardStore(tmp_path / "board.db")
    s.connect()
    yield s
    s.close()


def _card(store, **fields):
    payload = dict(title="Ship it", root="/project", tool="claude")
    payload.update(fields)
    c, reason = store.create(payload)
    assert c, reason
    return c


def _dispatch(store, cid, sid, at, provider="claude"):
    """The daemon's own two writes at a bind: `bind_session` and the
    `outcome_runs` row `_record_outcome_binding` adds beside it."""
    store.update(cid, {"link_state": "dispatching", "dispatched_at": at,
                       "session_id": ""}, bump=False)
    store.bind_session(cid, sid)
    store.record_outcome_run(cid, provider, sid, "implementation")


def _timed_out_dispatch(store, cid, at):
    """`_bind_dispatched_card`'s give-up: the window ran out, nothing bound,
    the card goes back with an orange line. `lifecycle_attempts` still holds
    a row for it; `outcome_runs` never sees it."""
    store.update(cid, {"link_state": "dispatching", "dispatched_at": at,
                       "session_id": ""}, bump=False)
    store.update(cid, {"column_name": "backlog", "link_state": "",
                       "dispatched_at": None,
                       "dispatch_error": "no session appeared"}, bump=False)


def test_run_health_attempts_equal_run_figures_attempts_on_one_store(store):
    """One tile draws both lines, so one word means one count: a dispatch
    that timed out unbound is not an attempt on either, and the retry that
    bound is one on both — while `lifecycle_attempts`, which the first cut
    counted, holds two rows."""
    c = _card(store)
    _timed_out_dispatch(store, c["id"], 1_000.0)
    _dispatch(store, c["id"], "sid-2", 1_200.0)
    health = store.run_health_counts([c["id"]])[c["id"]]["attempts"]
    figures = store.run_figures()[c["id"]]["attempts"]
    assert health == figures == 1
    rows = store._conn.execute(
        "SELECT COUNT(*) AS n FROM lifecycle_attempts WHERE card_id=?"
        " AND phase='implementation'", (c["id"],)).fetchone()["n"]
    assert rows == 2
    # A second bound session is a second attempt on both.
    store.mark_ended(c["id"], when=1_300.0)
    store.update(c["id"], {"session_id": "", "link_state": ""})
    _dispatch(store, c["id"], "sid-3", 1_400.0)
    assert (store.run_health_counts([c["id"]])[c["id"]]["attempts"]
            == store.run_figures()[c["id"]]["attempts"] == 2)


def test_run_health_counts_two_dispatches_are_two_attempts(store):
    c = _card(store)
    _dispatch(store, c["id"], "sid-1", 1_000.0)
    store.mark_ended(c["id"], when=1_010.0)
    store.update(c["id"], {"session_id": "", "link_state": ""})
    _dispatch(store, c["id"], "sid-2", 1_020.0)
    assert store.run_health_counts([c["id"]]) == {
        c["id"]: {"attempts": 2, "returns": 0}}


def test_run_health_counts_a_resumed_session_is_not_a_second_attempt(store):
    c = _card(store)
    _dispatch(store, c["id"], "sid-1", 1_000.0)
    store.mark_ended(c["id"], when=1_010.0)
    store.mark_live(c["id"])
    assert store.run_health_counts([c["id"]])[c["id"]]["attempts"] == 1


def test_run_health_counts_a_done_reopen_is_a_return(store):
    c = _card(store)
    _dispatch(store, c["id"], "sid-1", 1_000.0)
    store.record_outcome_run(c["id"], "claude", "sid-1", "implementation")
    store.mark_ended(c["id"], when=1_010.0)
    store.update(c["id"], {"column_name": "done"})
    store.update(c["id"], {"column_name": "in_progress"})
    counts = store.run_health_counts([c["id"]])[c["id"]]
    assert counts["returns"] == 1
    assert counts["attempts"] == 1


def test_run_health_counts_an_unknown_id_reads_zero(store):
    assert store.run_health_counts(["nope"]) == {"nope": {"attempts": 0, "returns": 0}}
    assert store.run_health_counts([]) == {}


# ── the decoration and the freeze ────────────────────────────────────────────

@pytest.fixture
def daemon(tmp_path, store):
    d = BobDaemon(headless=True, sessions_path=tmp_path / "sessions.json")
    d._board = store
    return d


def _snapshot(*rows):
    return {"running": list(rows), "waiting": [], "sleeping": [], "finished": []}


def test_a_card_with_a_live_row_carries_run_health(daemon, store, ledger_path):
    c = _card(store)
    _dispatch(store, c["id"], "s1", 1_000.0)
    daemon._agents_snapshot_cache = _snapshot(_row(session_id="s1"))
    out = daemon._decorate_card_for_snapshot(
        store.get(c["id"]), {}, set(), {}, set(), {}, set(),
        run_counts=store.run_health_counts([c["id"]]),
        run_ledger=rh.Ledger.load(ledger_path))
    assert out["run_health"]["live"] is True
    assert out["run_health"]["attempts"] == 1
    assert out["run_health"]["class"] == "large"
    assert out["run_health"]["turns"] == 80


def test_a_card_with_no_session_and_no_entry_carries_no_key(daemon, store, ledger_path):
    c = _card(store)
    daemon._agents_snapshot_cache = _snapshot()
    out = daemon._decorate_card_for_snapshot(
        store.get(c["id"]), {}, set(), {}, set(), {}, set(),
        run_counts={}, run_ledger=rh.Ledger.load(ledger_path))
    assert "run_health" not in out


def test_a_card_whose_session_is_gone_but_frozen_reads_the_ledger(
        daemon, store, ledger_path):
    c = _card(store)
    _dispatch(store, c["id"], "s1", 1_000.0)
    ledger = rh.Ledger.load(ledger_path)
    ledger.freeze(c["id"], "/project", _reading(turns=15), 1_000.0)
    daemon._agents_snapshot_cache = _snapshot()
    out = daemon._decorate_card_for_snapshot(
        store.get(c["id"]), {}, set(), {}, set(), {}, set(),
        run_counts=store.run_health_counts([c["id"]]), run_ledger=ledger)
    assert out["run_health"]["live"] is False
    assert out["run_health"]["turns"] == 15
    assert out["run_health"]["attempts"] == 1


def test_the_frame_reads_the_counts_once_and_stamps_every_card(
        daemon, store, ledger_path, monkeypatch):
    monkeypatch.setattr(rh.paths, "RUN_HEALTH_PATH", ledger_path)
    c = _card(store)
    _dispatch(store, c["id"], "s1", 1_000.0)
    other = _card(store, title="idle")
    daemon._agents_snapshot_cache = _snapshot(_row(session_id="s1"))
    calls = []
    real = store.run_health_counts

    def counted(ids):
        calls.append(list(ids))
        return real(ids)

    monkeypatch.setattr(store, "run_health_counts", counted)
    state = daemon._build_board_state()
    by_id = {card["id"]: card for card in state["cards"]}
    assert "run_health" in by_id[c["id"]]
    assert "run_health" not in by_id[other["id"]]
    assert len(calls) == 1 and set(calls[0]) >= {c["id"], other["id"]}
    assert state["run_health_supported"] is True


def test_the_freeze_writes_the_final_reading_once(daemon, store, ledger_path, monkeypatch):
    monkeypatch.setattr(rh.paths, "RUN_HEALTH_PATH", ledger_path)
    c = _card(store)
    _dispatch(store, c["id"], "s1", 1_000.0)
    snapshot = _snapshot(_row(session_id="s1"))
    daemon._freeze_run_health(store.get(c["id"]), snapshot)
    entry = rh.Ledger.load(ledger_path).entry_for(c["id"])
    assert entry["dispatched_at"] == 1_000.0
    assert entry["root"] == "/project"
    assert entry["reading"]["turns"] == 80 and entry["reading"]["live"] is False
    mtime = ledger_path.stat().st_mtime_ns
    daemon._freeze_run_health(store.get(c["id"]), snapshot)
    assert ledger_path.stat().st_mtime_ns == mtime


def test_the_freeze_needs_a_row_and_a_dispatch(daemon, store, ledger_path, monkeypatch):
    monkeypatch.setattr(rh.paths, "RUN_HEALTH_PATH", ledger_path)
    c = _card(store)
    _dispatch(store, c["id"], "s1", 1_000.0)
    daemon._freeze_run_health(store.get(c["id"]), _snapshot())
    assert not ledger_path.exists()
    daemon._freeze_run_health({"id": c["id"], "session_id": "s1", "root": "/project"},
                              _snapshot(_row(session_id="s1")))
    assert not ledger_path.exists()


def test_the_reconcile_freezes_at_the_ended_seam(daemon, store, ledger_path, monkeypatch):
    """`_consider_work_record`'s seam, one line on: a card whose session has
    gone quiet past the grace is marked ended and its reading frozen."""
    monkeypatch.setattr(rh.paths, "RUN_HEALTH_PATH", ledger_path)
    c = _card(store)
    _dispatch(store, c["id"], "s1", 1_000.0)
    store.mark_live(c["id"])
    snapshot = {"running": [], "waiting": [], "sleeping": [],
                "finished": [_row(session_id="s1")]}
    daemon._board_missing_since[c["id"]] = time.time() - daemon.BOARD_SESSION_GRACE - 1
    daemon._reconcile_board(snapshot)
    assert store.get(c["id"])["link_state"] == "ended"
    assert rh.Ledger.load(ledger_path).entry_for(c["id"]) is not None


def test_the_marker_rides_pipeline_writable(daemon):
    assert daemon._pipeline_writable()["run_health_supported"] is True


# ── the frame the reading buys ───────────────────────────────────────────────

def _turns(n, sid="s1"):
    row = _row(session_id=sid)
    row["stats"]["assistant_messages"] = n
    return _snapshot(row)


def test_a_quantum_of_turns_buys_a_frame_and_a_turn_inside_one_does_not(
        daemon, store, ledger_path, monkeypatch):
    """The detector composes the same dict the snapshot publishes, so the
    quantisation inside `compose` is the whole bound: 20 → 25 is a change
    the board draws; 21 → 22 is not."""
    monkeypatch.setattr(rh.paths, "RUN_HEALTH_PATH", ledger_path)
    c = _card(store)
    _dispatch(store, c["id"], "s1", 1_000.0)
    cards = store.cards()
    assert daemon._run_health_drifted(cards, _turns(20)) is True
    assert daemon._run_health_drifted(cards, _turns(20)) is False
    assert daemon._run_health_seen[c["id"]]["turns"] == 20
    # 21 and 22 both floor to 20: no frame.
    assert daemon._run_health_drifted(cards, _turns(21)) is False
    assert daemon._run_health_drifted(cards, _turns(22)) is False
    assert daemon._run_health_drifted(cards, _turns(25)) is True
    assert daemon._run_health_seen[c["id"]]["turns"] == 25
    assert daemon._run_health_drifted(cards, _turns(27)) is False
    # Tokens and context are bounded the same way.
    snap = _turns(22)
    snap["running"][0]["metrics"] = {"ctx_used_pct": 71}
    assert daemon._run_health_drifted(cards, snap) is True
    snap["running"][0]["metrics"] = {"ctx_used_pct": 73}
    assert daemon._run_health_drifted(cards, snap) is False
    snap["running"][0]["metrics"] = {"ctx_used_pct": 75}
    assert daemon._run_health_drifted(cards, snap) is True


def test_the_reconcile_reports_the_moved_reading(daemon, store, ledger_path,
                                                 monkeypatch):
    """Through `_reconcile_board` itself: a snapshot whose only change is a
    row going 20 → 25 turns reports a change; 21 → 22 above 20 does not."""
    monkeypatch.setattr(rh.paths, "RUN_HEALTH_PATH", ledger_path)
    c = _card(store)
    _dispatch(store, c["id"], "s1", 1_000.0)
    store.mark_live(c["id"])
    monkeypatch.setattr(daemon, "_claiming_session_ids", lambda: {"s1"})
    # The sibling's own drift term has its own tests; hold it still.
    daemon._run_figures_drifted = lambda: False
    daemon._reconcile_board(_turns(20))
    assert daemon._reconcile_board(_turns(20)) is False
    assert daemon._reconcile_board(_turns(21)) is False
    assert daemon._reconcile_board(_turns(22)) is False
    assert daemon._reconcile_board(_turns(25)) is True
    assert daemon._reconcile_board(_turns(26)) is False
    assert daemon._reconcile_board(_turns(30)) is True


def test_an_unbound_card_and_a_broken_store_buy_nothing(daemon, store, monkeypatch):
    c = _card(store)
    assert daemon._run_health_drifted(store.cards(), _turns(20)) is False
    assert daemon._run_health_seen == {}
    _dispatch(store, c["id"], "s1", 1_000.0)
    cards = store.cards()
    monkeypatch.setattr(store, "run_health_counts",
                        lambda ids: (_ for _ in ()).throw(OSError("disk")))
    assert daemon._run_health_drifted(cards, _turns(20)) is False


def test_a_card_gone_from_the_board_leaves_the_memo(daemon, store, ledger_path,
                                                   monkeypatch):
    monkeypatch.setattr(rh.paths, "RUN_HEALTH_PATH", ledger_path)
    c = _card(store)
    _dispatch(store, c["id"], "s1", 1_000.0)
    assert daemon._run_health_drifted(store.cards(), _turns(20)) is True
    assert c["id"] in daemon._run_health_seen
    store.delete(c["id"])
    assert daemon._run_health_drifted(store.cards(), _turns(20)) is False
    assert daemon._run_health_seen == {}


def test_the_drift_check_is_in_the_reconcile_after_the_ended_seam():
    src = (REPO / "host" / "dark_army_daemon" / "daemon_board.py").read_text()
    body = src.split("def _reconcile_board(", 1)[1].split("\n    def ", 1)[0]
    assert "self._run_health_drifted(cards, snapshot)" in body
    assert body.index("_freeze_run_health") < body.index("_run_health_drifted")


def test_a_resumed_run_freezes_its_later_reading(daemon, store, ledger_path,
                                                 monkeypatch):
    """`ended → live` keeps `dispatched_at`; the second quiet spell's
    reading — more turns — replaces the first in the ledger, and the same
    reading again is one entry."""
    monkeypatch.setattr(rh.paths, "RUN_HEALTH_PATH", ledger_path)
    c = _card(store)
    _dispatch(store, c["id"], "s1", 1_000.0)
    daemon._freeze_run_health(store.get(c["id"]), _turns(20))
    assert rh.Ledger.load(ledger_path).entry_for(c["id"])["reading"]["turns"] == 20
    store.mark_ended(c["id"], when=1_010.0)
    store.mark_live(c["id"])
    assert store.get(c["id"])["dispatched_at"] == 1_000.0
    daemon._freeze_run_health(store.get(c["id"]), _turns(40))
    loaded = rh.Ledger.load(ledger_path)
    assert len(loaded.entries) == 1
    assert loaded.entry_for(c["id"])["reading"]["turns"] == 40
    mtime = ledger_path.stat().st_mtime_ns
    daemon._freeze_run_health(store.get(c["id"]), _turns(40))
    assert ledger_path.stat().st_mtime_ns == mtime


# ── source pins ──────────────────────────────────────────────────────────────

def _read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


def test_the_phone_copy_of_the_line_is_byte_equal():
    assert (REPO / "ios/BobPhone/RunHealth.swift").read_bytes() == \
        (REPO / "panel/Sources/BobPanel/RunHealth.swift").read_bytes()


def test_run_health_reads_spawn_counts_and_neither_deduped_list():
    src = _read("host/dark_army_daemon/run_health.py")
    assert "spawn_counts" in src
    assert "subagents_seen" not in src
    assert "agent_trail" not in src
    assert "sys.platform" not in src
    assert not re.search(r"\bcost\b|\bduration\b", src)


def test_log_permission_names_none_of_the_counters():
    """The one tempting funnel runs on the executor under `_reap_permissions`
    and must not write `_session_states`."""
    src = _read("host/dark_army_daemon/daemon.py")
    start = src.index("    def _log_permission(")
    end = src.index("\n    def ", start + 10)
    body = src[start:end]
    for key in rh.COUNTER_KEYS:
        assert key not in body
    assert "_count_on_session" not in body


@pytest.mark.parametrize("rel", [
    "panel/Sources/BobPanel/BoardCardView.swift",
    "panel/Sources/BobPanel/BoardCardSheet.swift",
    "ios/BobPhone/CardDetailView.swift",
])
def test_every_surface_draws_the_shared_line_and_re_derives_no_class(rel):
    src = _read(rel)
    assert "RunHealthLine.text(" in src
    assert "RunHealthLine.spoken(" in src
    assert '"worrying"' not in src


def test_the_inbox_knows_nothing_of_run_health():
    for rel in ("panel/Sources/BobPanel/Inbox.swift", "ios/BobPhone/Inbox.swift"):
        assert "runHealth" not in _read(rel)


def test_the_phone_gates_on_the_marker_and_the_panel_does_not():
    phone = _read("ios/BobPhone/CardDetailView.swift")
    assert "board.runHealthSupported" in phone
    assert 'case runHealthSupported = "run_health_supported"' in _read("ios/BobPhone/Models.swift")
    panel = _read("panel/Sources/BobPanel/BoardModels.swift")
    assert "runHealthSupported" not in panel
    assert 'case runHealth = "run_health"' in panel


def test_the_ledger_file_is_in_the_private_ring():
    from dark_army_daemon import paths
    assert paths.RUN_HEALTH_PATH.name == "run-health.json"
    assert "run-health.json" in paths._PRIVATE_FILES


def test_effort_compose_reads_the_observed_effort_off_the_row():
    row = _row()
    row["stats"]["effort"] = "high"
    out = rh.compose(row, {}, None, DEFAULT_CUTS)
    assert out["effort"] == "high"


def test_effort_compose_without_one_is_empty_not_absent():
    row = _row()
    row["stats"].pop("effort", None)
    assert rh.compose(row, {}, None, DEFAULT_CUTS)["effort"] == ""
    row["stats"]["effort"] = None
    assert rh.compose(row, {}, None, DEFAULT_CUTS)["effort"] == ""


def test_effort_a_frozen_entry_keeps_it_and_an_old_one_reads_empty():
    ledger = rh.Ledger()
    row = _row()
    row["stats"]["effort"] = "xhigh"
    reading = rh.compose(row, {}, None, DEFAULT_CUTS)
    assert ledger.freeze("c1", "/r", reading, 1.0, now=10.0)
    frozen = rh.compose(None, {"attempts": 1, "returns": 0},
                        ledger.entry_for("c1"), DEFAULT_CUTS)
    assert frozen["effort"] == "xhigh" and frozen["live"] is False
    old = {"card_id": "c2", "root": "/r", "at": 1.0, "dispatched_at": 1.0,
           "reading": {"class": "typical", "turns": 5}}
    out = rh.compose(None, {}, old, DEFAULT_CUTS)
    assert out["effort"] == ""
