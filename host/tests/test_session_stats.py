"""Tests for the per-session transcript stats parser."""
import json

import pytest

from dark_army_daemon.session_stats import parse_transcript, SessionStats


def _write(tmp_path, lines):
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8")
    return str(p)


def _assistant(ts, model, usage, content=None):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"model": model, "usage": usage, "content": content or []},
    }


def test_missing_file_returns_empty_stats():
    s = parse_transcript("/no/such/file.jsonl")
    assert isinstance(s, SessionStats)
    assert s.assistant_messages == 0
    assert s.model is None
    assert s.duration_seconds == 0.0


def test_empty_path_returns_empty_stats():
    assert parse_transcript("").assistant_messages == 0


def test_rolls_up_tokens_model_tools_and_turns(tmp_path):
    path = _write(tmp_path, [
        {"type": "user", "timestamp": "2026-07-25T10:00:00.000Z",
         "message": {"role": "user", "content": "start"}},
        _assistant(
            "2026-07-25T10:00:05.000Z", "claude-opus-4-8",
            {"input_tokens": 100, "output_tokens": 50,
             "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 300},
            content=[
                {"type": "thinking"},
                {"type": "tool_use", "name": "Bash"},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "/a.py"}},
            ],
        ),
        # a tool-result carrier user message — must NOT count as a prompt
        {"type": "user", "timestamp": "2026-07-25T10:00:06.000Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
        _assistant(
            "2026-07-25T10:02:00.000Z", "claude-opus-4-8",
            {"input_tokens": 20, "output_tokens": 30,
             "cache_read_input_tokens": 500, "cache_creation_input_tokens": 0},
            content=[
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "/a.py"}},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/b.py"}},
                {"type": "text", "text": "done"},
            ],
        ),
    ])
    s = parse_transcript(path)

    assert s.model == "claude-opus-4-8"
    assert s.models == ["claude-opus-4-8"]
    assert s.assistant_messages == 2
    assert s.user_prompts == 1                      # tool_result message excluded
    assert s.input_tokens == 120
    assert s.output_tokens == 80
    assert s.cache_read_tokens == 2500
    assert s.cache_creation_tokens == 300
    assert s.total_input_tokens == 120 + 300
    assert s.tool_counts == {"Bash": 1, "Edit": 2, "Read": 1}
    assert s.total_tool_calls == 4
    assert s.files_touched == 1                       # only /a.py edited; /b.py was Read
    assert s.duration_seconds == 120.0                # 10:00:00 → 10:02:00
    assert s.output_tokens_per_sec == 80 / 120.0


def test_synthetic_model_is_ignored(tmp_path):
    path = _write(tmp_path, [
        _assistant("2026-07-25T10:00:00.000Z", "<synthetic>",
                   {"output_tokens": 999}, content=[{"type": "tool_use", "name": "Bash"}]),
        _assistant("2026-07-25T10:00:01.000Z", "claude-sonnet-5",
                   {"input_tokens": 5, "output_tokens": 7}),
    ])
    s = parse_transcript(path)
    assert s.models == ["claude-sonnet-5"]
    assert s.assistant_messages == 1                  # synthetic not counted
    assert s.output_tokens == 7                       # synthetic tokens excluded
    assert "Bash" not in s.tool_counts                # synthetic tools excluded


def test_primary_model_is_most_used(tmp_path):
    path = _write(tmp_path, [
        _assistant("2026-07-25T10:00:00.000Z", "claude-sonnet-5", {"output_tokens": 1}),
        _assistant("2026-07-25T10:00:01.000Z", "claude-opus-4-8", {"output_tokens": 1}),
        _assistant("2026-07-25T10:00:02.000Z", "claude-opus-4-8", {"output_tokens": 1}),
    ])
    s = parse_transcript(path)
    assert s.model == "claude-opus-4-8"               # 2 vs 1
    assert set(s.models) == {"claude-opus-4-8", "claude-sonnet-5"}


def test_malformed_lines_are_skipped(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        "not json\n"
        + json.dumps(_assistant("2026-07-25T10:00:00.000Z", "claude-opus-4-8",
                                {"output_tokens": 42})) + "\n"
        + "{bad\n",
        encoding="utf-8",
    )
    s = parse_transcript(str(p))
    assert s.output_tokens == 42
    assert s.assistant_messages == 1


# --- Snapshot helpers -------------------------------------------------------

from dark_army_daemon.session_stats import (
    resolve_transcript, StatsCache, categorize, stats_to_dict,
    WAITING_HYSTERESIS_SECONDS,
)


def test_categorize_buckets():
    assert categorize("working", 0, False) == "running"
    assert categorize("thinking", 0, False) == "running"
    assert categorize("idle", 1, False) == "running"        # subagent → running
    assert categorize("idle", 0, False) == "sleeping"
    assert categorize("idle", 0, True) == "waiting"          # notification wins
    assert categorize("waiting", 0, False) == "waiting"
    assert categorize("error", 0, False) == "waiting"
    # A snag waits on nobody unless a card stands beside it (22 Sep 2026, RC3).
    assert categorize("confused", 0, False) == "sleeping"
    assert categorize("confused", 0, True) == "waiting"


# Every input that produces `waiting` — the two states and the card, with a
# `confused` row beside its card — because the hysteresis is about the
# *bucket*, not about any one road into it.
_WAITING_INPUTS = [
    ("waiting", 0, False),
    ("confused", 0, True),
    ("error", 0, False),
    ("idle", 0, True),
]


@pytest.mark.parametrize("since", [None, 0.0, 2.9, 3.1, 100.0])
def test_a_snag_with_no_card_sleeps_at_any_age(since):
    """`confused` with no card is a failed tool call or a dismissed idle
    reminder: not waiting on a person, so it never banks as waiting and the
    hysteresis has nothing to veto."""
    assert categorize("confused", 0, False, seconds_since_event=since) == "sleeping"


@pytest.mark.parametrize("state, subs, card", _WAITING_INPUTS)
def test_a_fresh_event_vetoes_waiting_to_running(state, subs, card):
    """Something happened moments ago: the session is demonstrably active, and
    a 'needs you' that flickers with every subagent event is unclickable."""
    assert categorize(state, subs, card, seconds_since_event=2.9) == "running"


@pytest.mark.parametrize("state, subs, card", _WAITING_INPUTS)
def test_a_quiet_wait_stands(state, subs, card):
    assert categorize(state, subs, card, seconds_since_event=3.1) == "waiting"


@pytest.mark.parametrize("state, subs, card", _WAITING_INPUTS)
def test_no_clock_reading_means_no_hysteresis(state, subs, card):
    """The None default preserves every caller that has no clock to offer."""
    assert categorize(state, subs, card) == "waiting"
    assert categorize(state, subs, card, seconds_since_event=None) == "waiting"


def test_the_window_is_the_named_constant():
    assert categorize("waiting", 0, False,
                      seconds_since_event=WAITING_HYSTERESIS_SECONDS) == "waiting"


def test_hysteresis_never_touches_the_other_buckets():
    """The parameter gates one transition; running and sleeping are indifferent
    to how fresh the last event was."""
    assert categorize("working", 0, False, seconds_since_event=0.0) == "running"
    assert categorize("idle", 1, False, seconds_since_event=0.0) == "running"
    assert categorize("idle", 0, False, seconds_since_event=0.0) == "sleeping"
    assert categorize(None, 0, False, seconds_since_event=100.0) == "sleeping"


def test_resolve_transcript_globs_by_id(tmp_path):
    proj = tmp_path / "-Users-me-proj"
    proj.mkdir()
    t = proj / "abc-123.jsonl"
    t.write_text("{}\n")
    assert resolve_transcript("abc-123", projects_dir=tmp_path) == str(t)
    assert resolve_transcript("missing", projects_dir=tmp_path) == ""
    assert resolve_transcript("", projects_dir=tmp_path) == ""


def test_stats_cache_rereads_only_on_mtime_change(tmp_path, monkeypatch):
    path = _write(tmp_path, [
        _assistant("2026-07-25T10:00:00.000Z", "claude-opus-4-8", {"output_tokens": 5}),
    ])
    calls = {"n": 0}
    import dark_army_daemon.session_stats as ss
    real = ss._read_new

    def counting(acc, p, **kw):
        calls["n"] += 1
        return real(acc, p, **kw)

    monkeypatch.setattr(ss, "_read_new", counting)
    cache = ss.StatsCache()
    a = cache.get(path)
    b = cache.get(path)                 # same mtime → cached, no re-read
    assert a is b
    assert calls["n"] == 1
    # bump mtime → re-read
    os.utime(path, (a.duration_seconds + 1e9, a.duration_seconds + 1e9))
    cache.get(path)
    assert calls["n"] == 2


def test_stats_cache_reads_only_what_was_appended(tmp_path):
    """The point of the accumulator: a growing transcript costs its delta.

    A live session appends on every turn, so the mtime gate alone still meant
    re-reading the whole file every snapshot tick."""
    import dark_army_daemon.session_stats as ss

    path = tmp_path / "t.jsonl"
    first = _assistant("2026-07-25T10:00:00.000Z", "claude-opus-4-8",
                       {"output_tokens": 5})
    path.write_text(json.dumps(first) + "\n", encoding="utf-8")

    cache = ss.StatsCache()
    stats = cache.get(str(path))
    assert stats.output_tokens == 5
    first_offset = cache._cache[str(path)][1].offset
    assert first_offset == path.stat().st_size

    second = _assistant("2026-07-25T10:00:01.000Z", "claude-opus-4-8",
                        {"output_tokens": 7})
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(second) + "\n")
    os.utime(path, (2e9, 2e9))

    stats = cache.get(str(path))
    assert stats.output_tokens == 12     # both turns counted
    assert stats.assistant_messages == 2
    # and the second read resumed from where the first stopped
    assert cache._cache[str(path)][1].offset == path.stat().st_size


def test_stats_cache_restarts_when_transcript_is_truncated(tmp_path):
    """A shrunk file means truncation or replacement — the byte offset no longer
    means what it did, so the accumulator has to start over rather than skip."""
    import dark_army_daemon.session_stats as ss

    path = tmp_path / "t.jsonl"
    rows = [_assistant(f"2026-07-25T10:00:0{i}.000Z", "claude-opus-4-8",
                       {"output_tokens": 5}) for i in range(4)]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    cache = ss.StatsCache()
    assert cache.get(str(path)).assistant_messages == 4

    path.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    os.utime(path, (2e9, 2e9))
    assert cache.get(str(path)).assistant_messages == 1


def test_incremental_parse_matches_a_full_parse(tmp_path):
    """Feeding a transcript in arbitrary chunks must land exactly where reading
    it in one go does — including when a chunk boundary splits a record, and
    when an Agent spawn's tool_use and its tool_result end up in different
    chunks (the case ``_Accum.spawn_types`` exists for)."""
    import random
    import dark_army_daemon.session_stats as ss

    rows = []
    for i in range(40):
        rows.append({
            "type": "assistant",
            "timestamp": f"2026-07-25T10:00:{i:02d}.000Z",
            "message": {"model": "claude-opus-5", "content": [
                {"type": "tool_use", "id": f"toolu_{i}", "name": "Agent",
                 "input": {"subagent_type": "Explore", "description": f"d{i}"}},
                {"type": "tool_use", "id": f"e_{i}", "name": "Edit",
                 "input": {"file_path": f"/f{i % 7}.py"}},
            ], "usage": {"output_tokens": i, "input_tokens": 2}},
        })
        rows.append({
            "type": "user",
            "timestamp": f"2026-07-25T10:00:{i:02d}.500Z",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": f"toolu_{i}"}]},
            "toolUseResult": {"agentId": f"a{i}", "description": f"d{i}",
                              "resolvedModel": "claude-opus-5"},
        })
    raw = ("\n".join(json.dumps(r) for r in rows) + "\n").encode()

    whole = tmp_path / "whole.jsonl"
    whole.write_bytes(raw)
    full = parse_transcript(str(whole))

    piece = tmp_path / "piece.jsonl"
    piece.write_bytes(b"")
    cache = ss.StatsCache()
    rng = random.Random(1234)
    pos = 0
    inc = None
    while pos < len(raw):
        step = rng.randint(1, 900)          # deliberately splits records
        with piece.open("ab") as fh:
            fh.write(raw[pos:pos + step])
        pos += step
        os.utime(piece, (pos, pos))          # a fresh mtime for every chunk
        inc = cache.get(str(piece))

    assert inc is not None
    assert inc.assistant_messages == full.assistant_messages
    assert inc.output_tokens == full.output_tokens
    assert inc.input_tokens == full.input_tokens
    assert inc.user_prompts == full.user_prompts
    assert inc.files_touched == full.files_touched
    assert inc.tool_counts == full.tool_counts
    assert inc.models == full.models
    assert (inc.first_ts, inc.last_ts) == (full.first_ts, full.last_ts)
    # the spawn join survived the chunking
    assert sorted(inc.agents) == sorted(full.agents)
    assert all(inc.agents[a].subagent_type == "Explore" for a in inc.agents)


def test_transcript_resolution_caches_are_bounded(tmp_path, monkeypatch):
    """Both are keyed by session id in a daemon meant to run for weeks, so an
    unbounded dict gains an entry per session the machine ever runs.

    The miss cache needs its own sweep rather than relying on the TTL: an entry
    is only dropped when that session is asked about *again*, and the sessions
    that never come back are exactly the ones that never had a transcript."""
    import dark_army_daemon.session_stats as ss

    monkeypatch.setattr(ss, "_TRANSCRIPT_CACHE_MAX", 16)
    monkeypatch.setattr(ss, "_TRANSCRIPT_MISSES", {})
    monkeypatch.setattr(ss, "_TRANSCRIPT_PATHS", ss.OrderedDict())

    empty = tmp_path / "projects"
    empty.mkdir()
    for i in range(200):
        ss.resolve_transcript(f"missing-{i}", projects_dir=empty)
    assert len(ss._TRANSCRIPT_MISSES) <= 16

    proj = tmp_path / "p"
    (proj / "d").mkdir(parents=True)
    for i in range(200):
        (proj / "d" / f"found-{i}.jsonl").write_text("{}\n", encoding="utf-8")
        assert ss.resolve_transcript(f"found-{i}", projects_dir=proj)
    assert len(ss._TRANSCRIPT_PATHS) <= 16


def test_stats_cache_is_bounded(tmp_path):
    """Unbounded, this grew one entry per transcript the daemon ever saw."""
    import dark_army_daemon.session_stats as ss

    cache = ss.StatsCache(max_entries=3)
    for i in range(6):
        p = tmp_path / f"t{i}.jsonl"
        p.write_text(json.dumps(
            _assistant("2026-07-25T10:00:00.000Z", "claude-opus-4-8",
                       {"output_tokens": 1})) + "\n", encoding="utf-8")
        cache.get(str(p))
    assert len(cache._cache) == 3


def test_stats_to_dict_has_menu_fields(tmp_path):
    path = _write(tmp_path, [
        _assistant("2026-07-25T10:00:00.000Z", "claude-opus-4-8",
                   {"input_tokens": 3, "output_tokens": 7, "cache_read_input_tokens": 9}),
    ])
    d = stats_to_dict(parse_transcript(path))
    for key in ("model", "output_tokens", "total_input_tokens", "cache_read_tokens",
                "duration_seconds", "assistant_messages", "tool_counts",
                "files_touched", "output_tokens_per_sec"):
        assert key in d
    assert "first_ts" not in d          # datetimes dropped


import os


def test_detailed_snapshot_groups_and_attaches_stats(tmp_path, monkeypatch):
    import dark_army_daemon.session_stats as ss
    from dark_army_daemon.daemon import BobDaemon

    # one transcript, referenced by the "running" session
    tpath = _write(tmp_path, [
        _assistant("2026-07-25T10:00:00.000Z", "claude-opus-4-8", {"output_tokens": 42}),
    ])
    monkeypatch.setattr(ss, "resolve_transcript",
                        lambda sid, **kw: tpath if sid == "run1" else "")
    monkeypatch.setattr(ss, "session_display_name", lambda *a, **k: "x", raising=False)

    d = BobDaemon()
    d._session_states = {
        "run1": {"state": "working", "project": "proj-a", "last_event": 0, "tool_name": "Bash"},
        "sleep1": {"state": "idle", "project": "proj-b", "last_event": 0},
        "wait1": {"state": "idle", "project": "proj-c", "last_event": 0},
    }
    d._active_notifications = {"wait1": {"message": "hi"}}

    snap = d.detailed_snapshot()
    assert [a["session_id"] for a in snap["running"]] == ["run1"]
    assert [a["session_id"] for a in snap["sleeping"]] == ["sleep1"]
    assert [a["session_id"] for a in snap["waiting"]] == ["wait1"]
    # the running agent carries parsed transcript stats
    assert snap["running"][0]["stats"]["output_tokens"] == 42
    assert snap["running"][0]["current_tool"] == "Bash"
    # sessions without a transcript still appear, with zeroed stats
    assert snap["sleeping"][0]["stats"]["output_tokens"] == 0


# --- Subagent spawn records (labels for the menu's 4th section) --------------

import dark_army_daemon.session_stats as ss


def _spawn_transcript(tmp_path):
    """A parent transcript containing one Agent spawn: the assistant tool_use
    that carries subagent_type, then the tool_result that carries agentId +
    description. Claude Code writes both at launch, before the agent finishes."""
    import json
    rows = [
        {"type": "assistant", "message": {"model": "claude-opus-5", "content": [
            {"type": "tool_use", "id": "toolu_ABC", "name": "Agent",
             "input": {"subagent_type": "Explore", "description": "Verify LVGL"}}]}},
        {"type": "user",
         "message": {"content": [
             {"type": "tool_result", "tool_use_id": "toolu_ABC", "content": "ok"}]},
         "toolUseResult": {"agentId": "a37a95b644cf16ad9", "isAsync": True,
                           "status": "async_launched", "description": "Verify LVGL",
                           "resolvedModel": "claude-opus-5"}},
    ]
    p = tmp_path / "parent.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return str(p)


def test_parse_transcript_collects_subagent_spawn(tmp_path):
    stats = ss.parse_transcript(_spawn_transcript(tmp_path))
    info = stats.agents["a37a95b644cf16ad9"]
    assert info.description == "Verify LVGL"
    assert info.subagent_type == "Explore"       # joined via tool_use_id
    assert info.model == "claude-opus-5"


def test_spawn_record_is_available_before_the_agent_finishes(tmp_path):
    """status stays "async_launched" for the whole run and no second row is
    written at completion — so a *running* subagent is labellable."""
    stats = ss.parse_transcript(_spawn_transcript(tmp_path))
    assert "a37a95b644cf16ad9" in stats.agents


def test_spawn_without_matching_tool_use_still_yields_description(tmp_path):
    """If the tool_use row is missing (truncated/compacted transcript) the
    description still comes from the spawn record; only the type is lost."""
    import json
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps(
        {"type": "user",
         "message": {"content": [
             {"type": "tool_result", "tool_use_id": "toolu_GONE", "content": "ok"}]},
         "toolUseResult": {"agentId": "aXYZ", "description": "Orphan task"}}),
        encoding="utf-8")
    info = ss.parse_transcript(str(p)).agents["aXYZ"]
    assert info.description == "Orphan task"
    assert info.subagent_type == ""


def test_spawn_type_survives_the_synthetic_model_gate(tmp_path):
    """The assistant branch skips rows with a missing/synthetic model, since they
    carry no billable usage. Subagent typing must not ride on that decision — it
    would drop labels for a reason unrelated to labelling."""
    import json
    rows = [
        {"type": "assistant", "message": {"model": "<synthetic>", "content": [
            {"type": "tool_use", "id": "toolu_S", "name": "Task",
             "input": {"subagent_type": "Explore", "description": "d"}}]}},
        {"type": "user",
         "message": {"content": [
             {"type": "tool_result", "tool_use_id": "toolu_S", "content": "ok"}]},
         "toolUseResult": {"agentId": "aS", "description": "d"}},
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    stats = ss.parse_transcript(str(p))
    assert stats.agents["aS"].subagent_type == "Explore"
    assert stats.assistant_messages == 0        # gate still applies to accounting


def test_transcript_without_subagents_has_empty_agents(tmp_path):
    import json
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}),
                 encoding="utf-8")
    assert ss.parse_transcript(str(p)).agents == {}


# --- Effort / fast mode ------------------------------------------------------

def test_effort_and_fast_come_from_the_latest_turn(tmp_path):
    """Both are switchable mid-session (/fast, the effort picker), so the menu
    must report the last turn's settings, not the session's first or commonest."""
    rows = [
        {"type": "assistant", "timestamp": "2026-07-25T10:00:00.000Z",
         "effort": "high",
         "message": {"model": "claude-opus-4-8",
                     "usage": {"output_tokens": 1, "speed": "standard"}}},
        {"type": "assistant", "timestamp": "2026-07-25T10:01:00.000Z",
         "effort": "medium",
         "message": {"model": "claude-opus-4-8",
                     "usage": {"output_tokens": 1, "speed": "fast"}}},
    ]
    s = ss.parse_transcript(_write(tmp_path, rows))
    assert s.effort == "medium"
    assert s.fast is True


def test_effort_absent_leaves_it_empty(tmp_path):
    """Older transcripts predate the field; the menu just omits it."""
    s = ss.parse_transcript(_write(tmp_path, [
        _assistant("2026-07-25T10:00:00.000Z", "claude-opus-4-8", {"output_tokens": 1}),
    ]))
    assert s.effort == ""
    assert s.fast is False


# --- Nested subagents: labelled from the meta file beside their transcript ---
#
# Regression guard for agents that rendered as a bare hex id. A subagent spawned
# by another *subagent* writes its spawn record into that subagent's transcript,
# never into the session's — so parse_transcript, which only ever reads the
# session transcript, could not see it at any cost.

def _nest(tmp_path, agent_id, meta, model="claude-opus-5"):
    """Lay out one subagent beside the `_spawn_transcript` session transcript,
    the way Claude Code does: <session-dir>/subagents/agent-<id>.{meta.json,jsonl}."""
    d = tmp_path / "parent" / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    lines = [{"type": "user", "message": {"content": "go"}}]
    if model:
        lines.append({"type": "assistant", "message": {"model": model, "content": []}})
    (d / f"agent-{agent_id}.jsonl").write_text(
        "\n".join(json.dumps(o) for o in lines), encoding="utf-8")
    return d


def _clear_meta_cache():
    ss._SUBAGENT_META_CACHE.clear()


def test_nested_subagent_is_named_from_its_meta_file(tmp_path):
    _clear_meta_cache()
    tpath = _spawn_transcript(tmp_path)
    _nest(tmp_path, "aNESTED", {
        "agentType": "Explore", "description": "Find table styling",
        "parentAgentId": "a37a95b644cf16ad9", "spawnDepth": 2})

    info = ss.load_subagent_meta(tpath, "aNESTED")
    assert info.description == "Find table styling"
    assert info.subagent_type == "Explore"
    assert info.model == "claude-opus-5"          # from the agent's own transcript
    assert info.parent_agent_id == "a37a95b644cf16ad9"


def test_subagent_with_no_meta_file_degrades_to_none(tmp_path):
    """Older Claude Code versions wrote no meta file; the menu falls back to the
    short id rather than the loader inventing a label."""
    _clear_meta_cache()
    assert ss.load_subagent_meta(_spawn_transcript(tmp_path), "aGHOST") is None


def test_meta_without_a_model_is_not_cached(tmp_path):
    """The meta file exists from the moment of spawn, but the model only appears
    with the agent's first turn. Caching that early would leave the agent
    modelless in the menu for the rest of its life."""
    _clear_meta_cache()
    tpath = _spawn_transcript(tmp_path)
    _nest(tmp_path, "aFRESH", {"agentType": "Explore", "description": "d"}, model="")
    assert ss.load_subagent_meta(tpath, "aFRESH").model == ""
    assert "aFRESH" not in ss._SUBAGENT_META_CACHE

    _nest(tmp_path, "aFRESH", {"agentType": "Explore", "description": "d"})
    assert ss.load_subagent_meta(tpath, "aFRESH").model == "claude-opus-5"


def test_snapshot_nests_a_subagents_own_subagent_under_it(tmp_path, monkeypatch):
    """The failing case from the menu: six live agents, four named from the
    session transcript and two — the ones spawned by an agent — bare ids."""
    from dark_army_daemon.daemon import BobDaemon
    _clear_meta_cache()

    tpath = _spawn_transcript(tmp_path)
    _nest(tmp_path, "aCHILD", {
        "agentType": "Explore", "description": "Dark mode conventions",
        "parentAgentId": "a37a95b644cf16ad9", "spawnDepth": 2})
    monkeypatch.setattr(ss, "resolve_transcript",
                        lambda sid, **kw: tpath if sid == "run1" else "")

    d = BobDaemon()
    d._session_states = {
        "run1": {"state": "working", "project": "p", "last_event": 0,
                 "subagents": {"a37a95b644cf16ad9", "aCHILD"}},
    }
    rows = d.detailed_snapshot()["running"][0]["subagent_rows"]
    # Parent first, then its child indented under it — not id-sorted, which
    # would interleave branches of a fan-out.
    assert [(r["description"], r["depth"]) for r in rows] == [
        ("Verify LVGL", 0),
        ("Dark mode conventions", 1),
    ]
    assert rows[1]["model"] == "claude-opus-5"


def test_subagent_rows_survive_a_parent_cycle(tmp_path):
    """A cycle in the parent links is impossible in practice, but dropping a
    live agent from the menu is worse than showing it flat."""
    from dark_army_daemon.daemon import BobDaemon
    _clear_meta_cache()

    class _Stats:
        agents = {
            "a": ss.AgentInfo(agent_id="a", description="A", parent_agent_id="b"),
            "b": ss.AgentInfo(agent_id="b", description="B", parent_agent_id="a"),
        }

    rows = BobDaemon._subagent_rows("", _Stats(), ["a", "b"])
    assert sorted(r["description"] for r in rows) == ["A", "B"]


# --- Snapshot shape: subagents nested, sections ordered ----------------------

def test_snapshot_nests_subagents_under_their_parent(tmp_path, monkeypatch):
    from dark_army_daemon.daemon import BobDaemon

    tpath = _spawn_transcript(tmp_path)
    monkeypatch.setattr(ss, "resolve_transcript",
                        lambda sid, **kw: tpath if sid == "run1" else "")
    monkeypatch.setattr(ss, "session_display_name", lambda *a, **k: "parent",
                        raising=False)

    d = BobDaemon()
    d._session_states = {
        "run1": {"state": "working", "project": "p", "last_event": 0,
                 "subagents": {"a37a95b644cf16ad9"}},
    }
    snap = d.detailed_snapshot()
    assert "subagents" not in snap              # no flat section any more
    rows = snap["running"][0]["subagent_rows"]
    assert [r["description"] for r in rows] == ["Verify LVGL"]
    assert rows[0]["model"] == "claude-opus-5"
    assert rows[0]["subagent_type"] == "Explore"


def test_snapshot_orders_each_section_by_what_you_act_on_first(monkeypatch):
    """Waiting: longest-blocked first. Running/sleeping: most recent first."""
    import time
    from dark_army_daemon.daemon import BobDaemon

    monkeypatch.setattr(ss, "resolve_transcript", lambda sid, **kw: "")
    now = time.time()
    d = BobDaemon()
    d._session_states = {
        "w-new": {"state": "idle", "project": "a", "last_event": now - 10},
        "w-old": {"state": "idle", "project": "b", "last_event": now - 900},
        "r-stale": {"state": "working", "project": "c", "last_event": now - 120},
        "r-fresh": {"state": "working", "project": "d", "last_event": now - 1},
    }
    d._active_notifications = {"w-new": {"message": "hi"}, "w-old": {"message": "hi"}}

    snap = d.detailed_snapshot()
    assert [a["session_id"] for a in snap["waiting"]] == ["w-old", "w-new"]
    assert [a["session_id"] for a in snap["running"]] == ["r-fresh", "r-stale"]


def test_transcript_path_is_memoised(tmp_path, monkeypatch):
    """The snapshot resolves every session on every refresh, and this globs every
    project directory. Watching 8 sessions on a 10s tick is 48 directory walks a
    minute for an answer that cannot change."""
    from dark_army_daemon import session_stats as ss

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "sid-1.jsonl").write_text("")
    ss._TRANSCRIPT_PATHS.clear()

    globs = []
    real_glob = ss.Path.glob

    def counting_glob(self, pattern):
        globs.append(pattern)
        return real_glob(self, pattern)

    monkeypatch.setattr(ss.Path, "glob", counting_glob)
    first = ss.resolve_transcript("sid-1", tmp_path)
    for _ in range(5):
        assert ss.resolve_transcript("sid-1", tmp_path) == first
    assert len(globs) == 1, f"globbed {len(globs)} times for one session"


def test_memo_is_dropped_when_the_transcript_disappears(tmp_path):
    """A deleted transcript must not keep being reported as present."""
    from dark_army_daemon import session_stats as ss

    proj = tmp_path / "proj"
    proj.mkdir()
    path = proj / "sid-2.jsonl"
    path.write_text("")
    ss._TRANSCRIPT_PATHS.clear()

    assert ss.resolve_transcript("sid-2", tmp_path) == str(path)
    path.unlink()
    assert ss.resolve_transcript("sid-2", tmp_path) == ""


# ── workspace facts: branch and cwd ──────────────────────────────────────────

def test_branch_and_cwd_are_captured(tmp_path):
    path = _write(tmp_path, [
        {"type": "user", "timestamp": "2026-08-14T10:00:00Z",
         "gitBranch": "main", "cwd": "/repos/bob"},
    ])
    stats = parse_transcript(path)
    assert stats.git_branch == "main"
    assert stats.cwd == "/repos/bob"


def test_latest_branch_wins(tmp_path):
    """A checkout mid-session moves the branch a row displays. It must not move
    the nickname — which is why identity keys on the directory alone."""
    path = _write(tmp_path, [
        {"type": "user", "timestamp": "2026-08-14T10:00:00Z", "gitBranch": "main"},
        {"type": "user", "timestamp": "2026-08-14T10:05:00Z", "gitBranch": "perf/x"},
    ])
    assert parse_transcript(path).git_branch == "perf/x"


def test_branch_is_read_before_the_synthetic_model_gate(tmp_path):
    """That gate exists for token accounting. Letting it decide whether we learn
    the branch would drop it for reasons that have nothing to do with the branch.
    """
    path = _write(tmp_path, [
        {"type": "assistant", "timestamp": "2026-08-14T10:00:00Z",
         "gitBranch": "main", "cwd": "/repos/bob",
         "message": {"model": "<synthetic>", "usage": {}, "content": []}},
    ])
    stats = parse_transcript(path)
    assert stats.git_branch == "main"
    assert stats.cwd == "/repos/bob"


def test_a_transcript_without_a_branch_says_so(tmp_path):
    path = _write(tmp_path, [{"type": "user", "timestamp": "2026-08-14T10:00:00Z"}])
    assert parse_transcript(path).git_branch == ""


def test_bad_branch_types_are_ignored(tmp_path):
    path = _write(tmp_path, [
        {"type": "user", "timestamp": "2026-08-14T10:00:00Z",
         "gitBranch": "main", "cwd": "/repos/bob"},
        {"type": "user", "timestamp": "2026-08-14T10:01:00Z",
         "gitBranch": 17, "cwd": None},
    ])
    stats = parse_transcript(path)
    assert stats.git_branch == "main"
    assert stats.cwd == "/repos/bob"


# --- The markers belong to a turn (S2, S5; 22 Sep 2026) ----------------------

_MARKED = ("Done: the ladder is tidy.\n"
           "<!-- bob-tldr: Tidied the strip ladder. -->\n"
           "<!-- bob-actions: Accept | Iterate -->")


def _prompt(ts, text="Tidy the strip ladder."):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": text}}


def _said(ts, text):
    return _assistant(ts, "claude-opus-5", {"output_tokens": 1},
                      [{"type": "text", "text": text}])


def _epoch(stamp):
    from datetime import datetime

    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def test_a_marker_is_stamped_with_its_own_clock(tmp_path):
    s = parse_transcript(_write(tmp_path, [
        _prompt("2026-09-22T10:00:00.000Z"),
        _said("2026-09-22T10:05:00.000Z", _MARKED),
    ]))
    assert s.last_summary == "Tidied the strip ladder."
    assert s.last_actions == ["Accept", "Iterate"]
    assert s.prompt_at == _epoch("2026-09-22T10:00:00.000Z")
    assert s.marker_at == _epoch("2026-09-22T10:05:00.000Z")
    assert s.marker_at > s.prompt_at


def test_the_next_prompt_blanks_the_markers_and_stamps_its_clock(tmp_path):
    """S2: last turn's caption and buttons answered last turn's request."""
    s = parse_transcript(_write(tmp_path, [
        _prompt("2026-09-22T10:00:00.000Z"),
        _said("2026-09-22T10:05:00.000Z", _MARKED),
        _prompt("2026-09-22T10:06:00.000Z", "Now the icons."),
    ]))
    assert s.last_summary == ""
    assert s.last_actions == []
    assert s.marker_at == 0.0
    assert s.prompt_at == _epoch("2026-09-22T10:06:00.000Z")


def test_a_tool_result_is_not_a_prompt_and_clears_nothing(tmp_path):
    s = parse_transcript(_write(tmp_path, [
        _prompt("2026-09-22T10:00:00.000Z"),
        _said("2026-09-22T10:05:00.000Z", _MARKED),
        {"type": "user", "timestamp": "2026-09-22T10:05:30.000Z",
         "message": {"content": [{"type": "tool_result",
                                  "tool_use_id": "toolu_1"}]}},
    ]))
    assert s.last_summary == "Tidied the strip ladder."
    assert s.last_actions == ["Accept", "Iterate"]
    assert s.prompt_at == _epoch("2026-09-22T10:00:00.000Z")


def test_the_next_turns_marker_sets_all_three_again(tmp_path):
    s = parse_transcript(_write(tmp_path, [
        _prompt("2026-09-22T10:00:00.000Z"),
        _said("2026-09-22T10:05:00.000Z", _MARKED),
        _prompt("2026-09-22T10:06:00.000Z", "Now the icons."),
        _said("2026-09-22T10:09:00.000Z",
              "Icons baked.\n<!-- bob-tldr: Baked the icons. -->\n"
              "<!-- bob-actions: Ship it -->"),
    ]))
    assert s.last_summary == "Baked the icons."
    assert s.last_actions == ["Ship it"]
    assert s.marker_at == _epoch("2026-09-22T10:09:00.000Z")
    assert s.marker_at >= s.prompt_at


def test_a_spoken_message_without_a_marker_resets_the_marker_clock(tmp_path):
    s = parse_transcript(_write(tmp_path, [
        _prompt("2026-09-22T10:00:00.000Z"),
        _said("2026-09-22T10:05:00.000Z", _MARKED),
        _said("2026-09-22T10:05:10.000Z", "One more thing, no marker."),
    ]))
    assert s.last_summary == "" and s.last_actions == []
    assert s.marker_at == 0.0


def test_a_line_with_no_timestamp_reads_zero(tmp_path):
    s = parse_transcript(_write(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": "go"}},
    ]))
    assert s.prompt_at == 0.0 and s.marker_at == 0.0


def test_stats_to_dict_carries_both_clocks(tmp_path):
    s = parse_transcript(_write(tmp_path, [
        _prompt("2026-09-22T10:00:00.000Z"),
        _said("2026-09-22T10:05:00.000Z", _MARKED),
    ]))
    out = stats_to_dict(s)
    assert out["marker_at"] == s.marker_at > 0.0
    assert out["prompt_at"] == s.prompt_at > 0.0


def test_the_incremental_cache_agrees_on_the_markers_and_clocks(tmp_path):
    """`StatsCache` folds only the appended lines: the S2 clearing and the S5
    stamps live in the shared per-record function, so a transcript grown
    turn by turn reads the same as a full scan at every step."""
    import os

    import dark_army_daemon.session_stats as ss

    turns = [
        [_prompt("2026-09-22T10:00:00.000Z")],
        [_said("2026-09-22T10:05:00.000Z", _MARKED)],
        [_prompt("2026-09-22T10:06:00.000Z", "Now the icons.")],
        [_said("2026-09-22T10:09:00.000Z",
               "Icons baked.\n<!-- bob-tldr: Baked the icons. -->")],
    ]
    piece = tmp_path / "piece.jsonl"
    piece.write_bytes(b"")
    cache = ss.StatsCache()
    so_far = []
    for step, rows in enumerate(turns, start=1):
        so_far.extend(rows)
        with piece.open("a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in rows))
        os.utime(piece, (step * 10, step * 10))
        inc = cache.get(str(piece))
        whole = tmp_path / f"whole-{step}.jsonl"
        whole.write_text("".join(json.dumps(r) + "\n" for r in so_far))
        full = parse_transcript(str(whole))
        for name in ("last_summary", "last_actions", "marker_at", "prompt_at"):
            assert getattr(inc, name) == getattr(full, name), (step, name)
    assert inc.last_summary == "Baked the icons."
    assert inc.last_actions == []
    assert inc.marker_at > inc.prompt_at > 0.0


# --- A slash command is not the person's next message (audit, 22 Sep 2026) --

def _compact_records(ts):
    """The four `user` records a `/compact` writes, in a real transcript's
    shapes: the summary, the meta caveat, the command echo and its output.
    None is a tool result, and none is the person asking anything."""
    return [
        {"type": "user", "timestamp": ts, "isCompactSummary": True,
         "isVisibleInTranscriptOnly": True,
         "message": {"role": "user", "content":
                     "This session is being continued from a previous conversation."}},
        {"type": "user", "timestamp": ts, "isMeta": True,
         "message": {"role": "user", "content":
                     "<local-command-caveat>Caveat: generated by a local command."
                     "</local-command-caveat>"}},
        {"type": "user", "timestamp": ts,
         "message": {"role": "user", "content":
                     "<command-name>/compact</command-name>\n"
                     "            <command-message>compact</command-message>\n"
                     "            <command-args></command-args>"}},
        {"type": "user", "timestamp": ts,
         "message": {"role": "user", "content":
                     "<local-command-stdout>Compacted</local-command-stdout>"}},
    ]


def test_a_compact_at_a_question_keeps_the_markers_and_the_prompt_clock(tmp_path):
    """Dark Army's own `autocompact.py` types `/compact` at a session parked
    on "Merge or wait?"; reading its records as a prompt wiped the question."""
    s = parse_transcript(_write(tmp_path, [
        _prompt("2026-09-22T10:00:00.000Z"),
        _said("2026-09-22T10:05:00.000Z", _MARKED),
        *_compact_records("2026-09-22T10:07:00.000Z"),
    ]))
    assert s.last_summary == "Tidied the strip ladder."
    assert s.last_actions == ["Accept", "Iterate"]
    assert s.marker_at == _epoch("2026-09-22T10:05:00.000Z")
    assert s.prompt_at == _epoch("2026-09-22T10:00:00.000Z")


@pytest.mark.parametrize("content", [
    "<command-name>/context</command-name>",
    "<local-command-stdout>Total cost: $1.20</local-command-stdout>",
    "<local-command-stderr>unknown command</local-command-stderr>",
    "  <task-notification>task b1 exited</task-notification>",
    [{"type": "text", "text": "<command-name>/cost</command-name>"}],
    [{"type": "text", "text": "<system-reminder>x</system-reminder>"}],
])
def test_other_local_command_records_are_not_a_prompt(tmp_path, content):
    s = parse_transcript(_write(tmp_path, [
        _prompt("2026-09-22T10:00:00.000Z"),
        _said("2026-09-22T10:05:00.000Z", _MARKED),
        {"type": "user", "timestamp": "2026-09-22T10:06:00.000Z",
         "message": {"role": "user", "content": content}},
    ]))
    assert s.last_actions == ["Accept", "Iterate"]
    assert s.prompt_at == _epoch("2026-09-22T10:00:00.000Z")


def test_a_real_prompt_after_a_compact_still_clears(tmp_path):
    s = parse_transcript(_write(tmp_path, [
        _prompt("2026-09-22T10:00:00.000Z"),
        _said("2026-09-22T10:05:00.000Z", _MARKED),
        *_compact_records("2026-09-22T10:07:00.000Z"),
        _prompt("2026-09-22T10:08:00.000Z", "Merge it."),
    ]))
    assert s.last_summary == "" and s.last_actions == []
    assert s.marker_at == 0.0
    assert s.prompt_at == _epoch("2026-09-22T10:08:00.000Z")


def test_a_list_form_prompt_is_still_a_prompt(tmp_path):
    s = parse_transcript(_write(tmp_path, [
        _prompt("2026-09-22T10:00:00.000Z"),
        _said("2026-09-22T10:05:00.000Z", _MARKED),
        {"type": "user", "timestamp": "2026-09-22T10:06:00.000Z",
         "message": {"role": "user", "content": [
             {"type": "text", "text": "Merge it, please."}]}},
    ]))
    assert s.last_actions == []
    assert s.prompt_at == _epoch("2026-09-22T10:06:00.000Z")


def test_the_incremental_cache_agrees_across_a_compact(tmp_path):
    import os

    import dark_army_daemon.session_stats as ss

    turns = [
        [_prompt("2026-09-22T10:00:00.000Z")],
        [_said("2026-09-22T10:05:00.000Z", _MARKED)],
        _compact_records("2026-09-22T10:07:00.000Z"),
        [_prompt("2026-09-22T10:08:00.000Z", "Merge it.")],
    ]
    piece = tmp_path / "piece.jsonl"
    piece.write_bytes(b"")
    cache = ss.StatsCache()
    so_far = []
    for step, rows in enumerate(turns, start=1):
        so_far.extend(rows)
        with piece.open("a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in rows))
        os.utime(piece, (step * 10, step * 10))
        inc = cache.get(str(piece))
        whole = tmp_path / f"whole-{step}.jsonl"
        whole.write_text("".join(json.dumps(r) + "\n" for r in so_far))
        full = parse_transcript(str(whole))
        for name in ("last_summary", "last_actions", "marker_at", "prompt_at"):
            assert getattr(inc, name) == getattr(full, name), (step, name)
        if step == 3:
            assert inc.last_actions == ["Accept", "Iterate"]


def test_the_not_a_prompt_prefixes_cover_the_title_readers():
    """`session_stats` cannot import `ai_title` (a cycle through
    `transcript_scan`), so the tuples are pinned to move together: every
    prefix the title readers skip is either never a prompt here or a command
    echo decided by its name — none is silently read as plain words."""
    from dark_army_daemon import ai_title
    from dark_army_daemon import session_stats as ss

    covered = set(ss._NOT_A_PROMPT_PREFIXES) | set(ss._COMMAND_ECHO_PREFIXES)
    assert set(ai_title._SYNTHETIC_PROMPT_PREFIXES) <= covered
    assert not set(ss._NOT_A_PROMPT_PREFIXES) & set(ss._COMMAND_ECHO_PREFIXES)
    for extra in ("<local-command-stderr>", "<bash-input>", "<bash-stdout>",
                  "<bash-stderr>"):
        assert extra in ss._NOT_A_PROMPT_PREFIXES
    assert "compact" in ss._LOCAL_COMMANDS


# --- A skill or custom command IS the person's request (audit 2) -----------

def _skill_records(ts, name="ship", args="implement plans/x.md"):
    """A skill call in a real transcript's shape: the echo, then the skill's
    expanded text flagged `isMeta` (as a list of blocks)."""
    return [
        {"type": "user", "timestamp": ts,
         "message": {"role": "user", "content":
                     f"<command-message>{name}</command-message>\n"
                     f"<command-name>/{name}</command-name>\n"
                     f"<command-args>{args}</command-args>"}},
        {"type": "user", "timestamp": ts, "isMeta": True,
         "message": {"role": "user", "content": [
             {"type": "text", "text": f"Base directory for this skill: /x/{name}\n\n# {name}"}]}},
    ]


@pytest.mark.parametrize("name, args", [
    ("ship", "implement plans/x.md"), ("review", ""), ("foo", "some args"),
    ("plugin:do-thing", "x"),
])
def test_a_skill_or_custom_command_clears_the_markers(tmp_path, name, args):
    """RC2 again: `/ship implement …` is a new request; an old offer left
    standing under it would buzz as a question until the agent spoke."""
    s = parse_transcript(_write(tmp_path, [
        _prompt("2026-09-22T10:00:00.000Z"),
        _said("2026-09-22T10:05:00.000Z", _MARKED),
        *_skill_records("2026-09-22T10:06:00.000Z", name, args),
    ]))
    assert s.last_summary == "" and s.last_actions == []
    assert s.marker_at == 0.0
    assert s.prompt_at == _epoch("2026-09-22T10:06:00.000Z")


@pytest.mark.parametrize("name", ["compact", "context", "cost", "model", "Status"])
def test_a_built_in_local_command_echo_is_not_a_prompt(tmp_path, name):
    s = parse_transcript(_write(tmp_path, [
        _prompt("2026-09-22T10:00:00.000Z"),
        _said("2026-09-22T10:05:00.000Z", _MARKED),
        {"type": "user", "timestamp": "2026-09-22T10:06:00.000Z",
         "message": {"role": "user", "content":
                     f"<command-message>{name}</command-message>\n"
                     f"<command-name>/{name}</command-name>\n"
                     "<command-args></command-args>"}},
    ]))
    assert s.last_actions == ["Accept", "Iterate"]
    assert s.prompt_at == _epoch("2026-09-22T10:00:00.000Z")


def test_the_incremental_cache_agrees_across_a_skill_call(tmp_path):
    import os

    import dark_army_daemon.session_stats as ss

    turns = [
        [_prompt("2026-09-22T10:00:00.000Z")],
        [_said("2026-09-22T10:05:00.000Z", _MARKED)],
        _compact_records("2026-09-22T10:06:00.000Z"),
        _skill_records("2026-09-22T10:07:00.000Z"),
    ]
    piece = tmp_path / "piece.jsonl"
    piece.write_bytes(b"")
    cache = ss.StatsCache()
    so_far = []
    for step, rows in enumerate(turns, start=1):
        so_far.extend(rows)
        with piece.open("a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in rows))
        os.utime(piece, (step * 10, step * 10))
        inc = cache.get(str(piece))
        whole = tmp_path / f"whole-{step}.jsonl"
        whole.write_text("".join(json.dumps(r) + "\n" for r in so_far))
        full = parse_transcript(str(whole))
        for name in ("last_summary", "last_actions", "marker_at", "prompt_at"):
            assert getattr(inc, name) == getattr(full, name), (step, name)
    assert inc.last_actions == []
    assert inc.prompt_at == _epoch("2026-09-22T10:07:00.000Z")


def test_a_shell_mode_command_is_not_a_prompt(tmp_path):
    """`! ls` writes its input and its output as two `user` records."""
    s = parse_transcript(_write(tmp_path, [
        _prompt("2026-09-22T10:00:00.000Z"),
        _said("2026-09-22T10:05:00.000Z", _MARKED),
        {"type": "user", "timestamp": "2026-09-22T10:06:00.000Z",
         "message": {"role": "user", "content": "<bash-input> ls</bash-input>"}},
        {"type": "user", "timestamp": "2026-09-22T10:06:01.000Z",
         "message": {"role": "user", "content":
                     "<bash-stdout>a b</bash-stdout><bash-stderr></bash-stderr>"}},
        {"type": "user", "timestamp": "2026-09-22T10:06:02.000Z",
         "message": {"role": "user", "content":
                     "<bash-stderr>no such file</bash-stderr>"}},
    ]))
    assert s.last_summary == "Tidied the strip ladder."
    assert s.last_actions == ["Accept", "Iterate"]
    assert s.prompt_at == _epoch("2026-09-22T10:00:00.000Z")
