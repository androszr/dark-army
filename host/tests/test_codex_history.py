# host/tests/test_codex_history.py
"""Codex journals into History's `turns`: the line shapes, the subset rule,
an unnamed model, a partial pass and an idempotent re-read.

The record shapes are Codex's own (`session_meta`, `turn_context`,
`event_msg` / `token_count`), trimmed to the fields the scanner reads.
"""

import json

import pytest

from dark_army_daemon import codex_history, codex_spenders
from dark_army_daemon.history import HistoryStore

THREAD = "01a0d99e-27bd-7203-bbb8-512583653522"


@pytest.fixture
def store(tmp_path):
    s = HistoryStore(tmp_path / "history.db")
    s.connect()
    yield s
    s.close()


@pytest.fixture
def sessions(tmp_path):
    root = tmp_path / "sessions"
    (root / "2026" / "09" / "25").mkdir(parents=True)
    return root


def _meta(thread=THREAD, ts="2026-09-25T17:30:15.000Z"):
    return {"timestamp": ts, "type": "session_meta",
            "payload": {"id": thread, "timestamp": ts, "cwd": "/p"}}


def _context(model="gpt-6-sol", ts="2026-09-25T17:30:18.000Z"):
    return {"timestamp": ts, "type": "turn_context", "payload": {"model": model}}


def _usage(inp, cached, out, reasoning=0, write=0):
    return {"input_tokens": inp, "cached_input_tokens": cached,
            "cache_write_input_tokens": write, "output_tokens": out,
            "reasoning_output_tokens": reasoning, "total_tokens": inp + out}


def _count(total, last, ts="2026-09-25T17:30:25.000Z"):
    info = {"total_token_usage": total, "model_context_window": 258400}
    if last is not None:
        info["last_token_usage"] = last
    return {"timestamp": ts, "type": "event_msg",
            "payload": {"type": "token_count", "info": info}}


def _write(sessions, lines, name=f"rollout-2026-09-25T19-30-15-{THREAD}.jsonl"):
    path = sessions / "2026" / "09" / "25" / name
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def _scan(store, sessions, **kwargs):
    kwargs.setdefault("journals", {})
    return codex_history.scan(store, sessions, **kwargs)


def _turns(store):
    return [dict(r) for r in store._query("SELECT * FROM turns ORDER BY ts")]


def test_a_token_count_line_becomes_one_codex_turn(store, sessions):
    first = _usage(17_779, 6_912, 154, reasoning=39)
    _write(sessions, [_meta(), _context(), _count(first, first)])
    summary = _scan(store, sessions)
    assert summary["turns_added"] == 1 and not summary["partial"]
    turn, = _turns(store)
    assert turn["session_id"] == f"codex:{THREAD}"
    assert turn["provider"] == "codex"
    assert turn["model"] == "gpt-6-sol"
    assert turn["input_tokens"] == 17_779
    assert turn["cache_read"] == 6_912            # a smaller subset, kept apart
    assert turn["output_tokens"] == 154           # reasoning is not added on top
    assert turn["cost_usd"] is None               # the journal states no dollar


def test_the_fixture_line_is_one_codex_spenders_also_accepts():
    """Both readers share one subset rule, so they cannot drift on it."""
    last = _usage(17_779, 6_912, 154, reasoning=39)
    assert codex_spenders._tokens(last) == (17_779, 154)
    assert codex_history._usage(last)["cache_read"] == 6_912


def test_cached_tokens_are_not_added_again_by_the_pricer(store, sessions):
    first = _usage(1_000_000, 400_000, 100_000)
    _write(sessions, [_meta(), _context(), _count(first, first)])
    _scan(store, sessions)
    facts = store.session_efficiency([f"codex:{THREAD}"])[f"codex:{THREAD}"]
    expected = (600_000 * 2.00 + 400_000 * 0.20 + 100_000 * 10.00) / 1e6
    assert facts["token_cost_usd"] == pytest.approx(expected)
    assert "reported_cost_usd" not in facts       # Codex reports no dollar
    assert "measured_cost_usd" not in facts
    assert "estimated_cost_usd" not in facts      # never the Claude estimate


def test_a_turn_with_no_model_name_is_stored_and_stays_unpriced(store, sessions):
    first = _usage(1_000, 0, 10)
    _write(sessions, [_meta(), _count(first, first)])
    _scan(store, sessions)
    turn, = _turns(store)
    assert turn["model"] is None
    facts = store.session_efficiency([f"codex:{THREAD}"])[f"codex:{THREAD}"]
    assert "token_cost_usd" not in facts
    assert facts["token_unpriced_turns"] == 1


def test_a_second_scan_adds_no_second_row(store, sessions):
    first = _usage(1_000, 0, 10)
    path = _write(sessions, [_meta(), _context(), _count(first, first)])
    journals = {}
    assert _scan(store, sessions, journals=journals)["turns_added"] == 1
    assert _scan(store, sessions, journals=journals)["files_read"] == 0
    # A restart forgets the memory; a changed file is re-read from the start.
    path.touch()
    assert _scan(store, sessions, journals={})["turns_added"] == 0
    store.set_scan_position(str(path), 0.0, 0)
    assert _scan(store, sessions, journals={})["turns_added"] == 0
    assert len(_turns(store)) == 1


def test_an_appended_tail_is_read_from_where_the_last_pass_stopped(store, sessions):
    first = _usage(1_000, 0, 10)
    second_total = _usage(3_000, 500, 30)
    second_last = _usage(2_000, 500, 20)
    path = _write(sessions, [_meta(), _context(), _count(first, first)])
    journals = {}
    _scan(store, sessions, journals=journals)
    with path.open("a") as stream:
        stream.write(json.dumps(_count(second_total, second_last,
                                       ts="2026-09-25T17:31:00.000Z")) + "\n")
    summary = _scan(store, sessions, journals=journals)
    assert summary["turns_added"] == 1
    later = _turns(store)[-1]
    assert (later["input_tokens"], later["cache_read"], later["model"]) == (
        2_000, 500, "gpt-6-sol")


def test_a_repeated_reading_is_one_turn(store, sessions):
    """Codex repeats the same cumulative reading with a new timestamp."""
    first = _usage(1_000, 0, 10)
    _write(sessions, [_meta(), _context(), _count(first, first),
                      _count(first, first, ts="2026-09-25T17:30:40.000Z")])
    _scan(store, sessions)
    assert len(_turns(store)) == 1


def test_reasoning_larger_than_output_is_dropped_as_unreadable(store, sessions):
    bad = _usage(1_000, 0, 10, reasoning=11)
    _write(sessions, [_meta(), _context(), _count(_usage(1_000, 0, 11), bad)])
    summary = _scan(store, sessions)
    assert summary["unreadable"] == 1
    assert _turns(store) == []


def test_cached_plus_written_over_the_input_is_stored_but_unpriced(store, sessions):
    over = _usage(1_000, 800, 10, write=300)
    _write(sessions, [_meta(), _context(), _count(over, over)])
    _scan(store, sessions)
    turn, = _turns(store)
    assert (turn["cache_read"], turn["cache_creation"]) == (800, 300)
    facts = store.session_efficiency([f"codex:{THREAD}"])[f"codex:{THREAD}"]
    assert "token_cost_usd" not in facts
    assert facts["token_unpriced_turns"] == 1


def test_a_fork_s_copied_history_belongs_to_its_parent(store, sessions):
    first = _usage(1_000, 0, 10)
    _write(sessions, [_meta(ts="2026-09-25T18:00:00.000Z"), _context(),
                      _count(first, first, ts="2026-09-25T17:30:25.000Z")])
    _scan(store, sessions)
    assert _turns(store) == []


def test_a_missing_last_usage_is_the_difference_of_two_readings(store, sessions):
    first = _usage(1_000, 100, 10)
    second = _usage(2_500, 600, 25)
    _write(sessions, [_meta(), _context(), _count(first, first),
                      _count(second, None, ts="2026-09-25T17:31:00.000Z")])
    _scan(store, sessions)
    later = _turns(store)[-1]
    assert (later["input_tokens"], later["cache_read"],
            later["output_tokens"]) == (1_500, 500, 15)


def test_a_pass_that_stops_early_says_partial_and_prices_no_tail(store, sessions):
    lines = [_meta(), _context()]
    for n in range(1, 40):
        total = _usage(1_000 * n, 0, 10 * n)
        lines.append(_count(total, _usage(1_000, 0, 10),
                            ts=f"2026-09-25T18:{n // 60:02d}:{n % 60:02d}.000Z"))
    _write(sessions, lines)
    journals = {}
    summary = _scan(store, sessions, byte_budget=2_000, journals=journals)
    assert summary["partial"]
    read = len(_turns(store))
    assert 0 < read < 39
    # Nothing unread was stored as a zero-cost stand-in; the next passes
    # finish the file and the partial flag clears.
    for _ in range(100):
        summary = _scan(store, sessions, byte_budget=2_000, journals=journals)
        if not summary["partial"]:
            break
    assert not summary["partial"]
    assert len(_turns(store)) == 39


def test_no_codex_folder_is_an_empty_whole_read(store, tmp_path):
    summary = codex_history.scan(store, tmp_path / "absent", journals={})
    assert summary["turns_added"] == 0 and not summary["partial"]


def test_codex_turns_do_not_reach_the_usage_view(store, sessions):
    """`/api/usage` reads Codex through `codex_spenders`; `turns` must not add
    it a second time."""
    first = _usage(1_000, 0, 10)
    _write(sessions, [_meta(), _context(), _count(first, first)])
    _scan(store, sessions)
    turn, = _turns(store)
    groups = store.usage_provider_groups(turn["ts"] - 60, turn["ts"] + 60)
    assert all(group["provider"] != "codex" for group in groups)
    assert store.usage_attribution(since=turn["ts"] - 60)["turns"] == 0


def test_the_scanner_never_writes_a_session_cost(store, sessions):
    first = _usage(1_000_000, 0, 10)
    _write(sessions, [_meta(), _context(), _count(first, first)])
    _scan(store, sessions)
    assert store._query("SELECT * FROM sessions") == []


@pytest.mark.asyncio
async def test_the_daemon_scans_codex_after_grok_and_remembers_a_partial_pass(
        tmp_path, monkeypatch):
    """One executor hop each, Claude then Grok then Codex; a short pass is
    remembered so the History report can say so."""
    from dark_army_daemon import daemon as daemon_module
    from dark_army_daemon.daemon import BobDaemon

    d = BobDaemon()
    d._history = HistoryStore(tmp_path / "history.db")
    d._history.connect()
    order = []

    def fake(name, result):
        def run(store, *args, **kwargs):
            order.append(name)
            if name == "codex":
                d._running = False
            return result
        return run

    monkeypatch.setattr(daemon_module.transcript_scan, "scan", fake("claude", {}))
    monkeypatch.setattr(daemon_module.grok_scan, "scan", fake("grok", {}))
    monkeypatch.setattr(daemon_module.codex_history, "scan",
                        fake("codex", {"partial": True}))
    monkeypatch.setattr(daemon_module, "TRANSCRIPT_SCAN_INTERVAL", 0)
    d._running = True
    try:
        await d._transcript_scanner()
    finally:
        d._history.close()
    assert order == ["claude", "grok", "codex"]
    assert d._codex_history_partial is True


CHILD = "01a0d301-0407-7b21-bb66-a10a5a9b1ca9"


def _child_meta(ts="2026-09-25T17:30:15.000Z", session_id=THREAD):
    payload = {"id": CHILD, "parent_thread_id": THREAD, "timestamp": ts,
               "source": {"subagent": {"thread_spawn": {
                   "parent_thread_id": THREAD, "depth": 1}}}}
    if session_id is not None:
        payload["session_id"] = session_id
    return {"timestamp": ts, "type": "session_meta", "payload": payload}


@pytest.mark.parametrize("session_id", [THREAD, None])
def test_a_subagent_s_turns_ride_the_root_thread(store, sessions, session_id):
    """A spawned child writes its own journal, but its spend is the card's:
    Codex stamps the root as `session_id`, the parent id is the fallback."""
    parent = _usage(1_000, 0, 10)
    child = _usage(5_000, 1_000, 50)
    _write(sessions, [_meta(), _context(), _count(parent, parent)])
    _write(sessions, [_child_meta(session_id=session_id), _context(),
                      _count(child, child, ts="2026-09-25T17:31:00.000Z")],
           name=f"rollout-2026-09-25T19-31-00-{CHILD}.jsonl")
    journals = {}
    assert _scan(store, sessions, journals=journals)["turns_added"] == 2
    assert {t["session_id"] for t in _turns(store)} == {f"codex:{THREAD}"}
    assert all(CHILD not in t["session_id"] for t in _turns(store))
    assert any(CHILD in t["message_id"] for t in _turns(store))
    # Idempotent: a re-read from byte 0 inserts nothing more.
    assert _scan(store, sessions, journals={})["turns_added"] == 0
    facts = store.session_efficiency([f"codex:{THREAD}"])[f"codex:{THREAD}"]
    assert facts["input_tokens"] == 6_000


def test_a_thread_that_is_not_a_subagent_is_its_own_session():
    payload = {"id": CHILD, "session_id": THREAD}
    assert codex_history._root_thread(payload, CHILD) == CHILD
