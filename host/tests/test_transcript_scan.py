# host/tests/test_transcript_scan.py
"""Transcript backfill: dedup, incremental reads, per-turn pricing.

Record shapes below are taken from real Claude Code transcripts — several
records per API response sharing one `message.id`, with only the last carrying
settled usage.
"""

import json
import time

import pytest

from dark_army_daemon import transcript_scan
from dark_army_daemon.history import COST_ESTIMATED, COST_MEASURED, HistoryStore
from dark_army_daemon.transcript_scan import parse_turns, scan


def _user(session="s1", text="fix the sprite pipeline",
          ts="2026-07-26T11:59:00Z"):
    return json.dumps({
        "type": "user",
        "sessionId": session,
        "timestamp": ts,
        "message": {"content": text},
    })


def _ai_title(session="s1", title="Fix the sprite pipeline"):
    return json.dumps({
        "type": "ai-title",
        "sessionId": session,
        "aiTitle": title,
    })


@pytest.fixture
def store(tmp_path):
    s = HistoryStore(tmp_path / "history.db")
    s.connect()
    yield s
    s.close()


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    (root / "-Users-x-repo").mkdir(parents=True)
    return root


def _assistant(session="s1", message_id="m1", model="claude-opus-4-8",
               ts="2026-07-26T12:00:00Z", **usage):
    return json.dumps({
        "type": "assistant",
        "sessionId": session,
        "timestamp": ts,
        "message": {
            "id": message_id,
            "model": model,
            "content": usage.pop("content", []),
            "usage": {
                "input_tokens": usage.pop("input_tokens", 0),
                "output_tokens": usage.pop("output_tokens", 0),
                "cache_read_input_tokens": usage.pop("cache_read", 0),
                "cache_creation_input_tokens": usage.pop("cache_creation", 0),
                **usage,
            },
        },
    })


def _write(projects, name, records):
    path = projects / "-Users-x-repo" / f"{name}.jsonl"
    path.write_text("\n".join(records) + "\n")
    return path


# --- parsing and dedup -------------------------------------------------------


def test_last_record_per_message_id_wins():
    """Claude Code writes several records per response; only the last has the
    settled usage. Summing them multiplies the bill."""
    turns = parse_turns([
        _assistant(message_id="m1", output_tokens=10),
        _assistant(message_id="m1", output_tokens=50),
        _assistant(message_id="m1", output_tokens=120),
    ])
    assert len(turns) == 1
    assert turns[0]["output_tokens"] == 120


def test_records_without_a_message_id_are_all_kept():
    turns = parse_turns([
        _assistant(message_id="", output_tokens=5),
        _assistant(message_id="", output_tokens=7),
    ])
    assert len(turns) == 2


def test_zero_usage_records_are_not_turns():
    assert parse_turns([_assistant(message_id="m1")]) == []


def test_non_assistant_records_ignored():
    lines = [
        json.dumps({"type": "user", "sessionId": "s1", "message": {"usage": {}}}),
        json.dumps({"type": "system", "subtype": "turn_duration", "durationMs": 10}),
        _assistant(message_id="m1", output_tokens=1),
    ]
    assert len(parse_turns(lines)) == 1


def test_malformed_lines_are_skipped():
    turns = parse_turns(["{not json but has usage", "", _assistant(output_tokens=3)])
    assert len(turns) == 1


def test_records_without_a_session_or_timestamp_are_skipped():
    no_session = json.dumps({"type": "assistant", "timestamp": "2026-07-26T12:00:00Z",
                             "message": {"id": "m", "usage": {"output_tokens": 1}}})
    no_ts = json.dumps({"type": "assistant", "sessionId": "s1",
                        "message": {"id": "m2", "usage": {"output_tokens": 1}}})
    assert parse_turns([no_session, no_ts]) == []


def test_cache_ttl_split_is_read_when_present():
    """The 1-hour TTL costs 2x input and the 5-minute one 1.25x — collapsing them
    understates an hour-cached workload."""
    # Built explicitly: newer transcripts carry the flat total *and* the typed
    # split side by side, which the keyword helper can't express.
    record = json.dumps({
        "type": "assistant", "sessionId": "s1", "timestamp": "2026-07-26T12:00:00Z",
        "message": {"id": "m1", "model": "claude-opus-4-8", "usage": {
            "cache_creation_input_tokens": 300,
            "cache_creation": {"ephemeral_5m_input_tokens": 100,
                               "ephemeral_1h_input_tokens": 200},
        }},
    })
    turns = parse_turns([record])
    assert turns[0]["cache_write_5m"] == 100
    assert turns[0]["cache_write_1h"] == 200


def test_untyped_cache_total_is_priced_at_the_cheaper_ttl():
    """Older transcripts report only a total. Attributing it to the 5-minute rate
    means an unknown split cannot inflate the estimate."""
    turns = parse_turns([_assistant(message_id="m1", cache_creation=500)])
    assert turns[0]["cache_write_5m"] == 500
    assert turns[0]["cache_write_1h"] == 0


def test_tool_name_and_sidechain_captured():
    record = json.loads(_assistant(message_id="m1", output_tokens=1))
    record["isSidechain"] = True
    record["agentId"] = "a1"
    record["message"]["content"] = [{"type": "tool_use", "name": "Bash"}]
    turns = parse_turns([json.dumps(record)])
    assert turns[0]["tool_name"] == "Bash"
    assert turns[0]["is_sidechain"] is True
    assert turns[0]["agent_id"] == "a1"


# --- scanning ----------------------------------------------------------------


def test_scan_ingests_turns(store, projects):
    _write(projects, "s1", [_assistant(output_tokens=100, input_tokens=10)])
    summary = scan(store, projects)
    assert summary["turns_added"] == 1
    assert store._scalar("SELECT SUM(output_tokens) FROM turns") == 100


def test_rescanning_an_unchanged_file_reads_nothing(store, projects):
    _write(projects, "s1", [_assistant(output_tokens=100)])
    scan(store, projects)
    second = scan(store, projects)
    assert second["files_read"] == 0 and second["turns_added"] == 0


def test_only_the_appended_tail_is_parsed(store, projects, monkeypatch):
    path = _write(projects, "s1", [_assistant(message_id="m1", output_tokens=1)])
    scan(store, projects)

    with path.open("a") as handle:
        handle.write(_assistant(message_id="m2", output_tokens=2) + "\n")

    seen = []
    real = transcript_scan.parse_turns
    monkeypatch.setattr(transcript_scan, "parse_turns",
                        lambda lines: seen.append(len(lines)) or real(lines))
    scan(store, projects)
    assert seen == [1], f"re-read {seen} lines instead of just the new one"
    assert store._scalar("SELECT COUNT(*) FROM turns") == 2


def test_a_shrunk_transcript_is_rescanned_from_the_start(store, projects):
    """A file shorter than the line count we recorded is not the append-only file
    we were tracking — it rotated or was rewritten. Resuming from the old offset
    would skip past records that are now different ones."""
    path = _write(projects, "s1", [
        _assistant(message_id=f"m{i}", output_tokens=1) for i in range(5)
    ])
    scan(store, projects)
    assert store._scalar("SELECT COUNT(*) FROM turns") == 5

    # Rewritten, shorter, and with a message id the old file never had.
    path.write_text(_assistant(message_id="fresh", output_tokens=7) + "\n")
    import os
    os.utime(path, (time.time() + 10, time.time() + 10))

    summary = scan(store, projects)
    assert summary["files_read"] == 1
    assert store._scalar(
        "SELECT COUNT(*) FROM turns WHERE output_tokens = 7") == 1


def test_scan_reads_scan_state_once_for_the_whole_sweep(store, projects):
    """Nearly every file in a sweep is unchanged, and asking per file turned one
    sweep into a query per transcript."""
    for i in range(6):
        _write(projects, f"s{i}", [_assistant(session=f"s{i}", output_tokens=1)])
    scan(store, projects)

    calls = {"n": 0}
    real = store.scan_position

    def counting(p):
        calls["n"] += 1
        return real(p)

    store.scan_position = counting
    scan(store, projects)
    assert calls["n"] == 0, "per-file scan_position lookups are back"


def test_a_re_scan_never_duplicates(store, projects):
    """Idempotence is what makes the scanner safe to run on every launch."""
    _write(projects, "s1", [_assistant(message_id="m1", output_tokens=100)])
    scan(store, projects)
    store.set_scan_position(str(projects / "-Users-x-repo" / "s1.jsonl"), 0, 0)
    scan(store, projects)
    assert store._scalar("SELECT COUNT(*) FROM turns") == 1
    assert store._scalar("SELECT SUM(output_tokens) FROM turns") == 100


def test_a_shrunken_file_is_rescanned_from_the_start(store, projects):
    """A rotated or rewritten transcript is not the append-only file we recorded;
    reading from the old offset would land in the middle of a new document."""
    path = _write(projects, "s1", [_assistant(message_id=f"m{i}", output_tokens=1)
                                   for i in range(5)])
    scan(store, projects)
    time.sleep(0.02)
    path.write_text(_assistant(message_id="fresh", output_tokens=9) + "\n")
    scan(store, projects)
    assert store._query("SELECT * FROM turns WHERE message_id='fresh'")


def test_missing_projects_dir_is_not_fatal(store, tmp_path):
    assert scan(store, tmp_path / "nope")["files_seen"] == 0


# --- pricing integration -----------------------------------------------------


def test_session_cost_is_estimated_and_labelled(store, projects):
    _write(projects, "s1", [_assistant(model="claude-opus-4-8",
                                       input_tokens=1_000_000,
                                       output_tokens=1_000_000)])
    scan(store, projects)
    row = store._query("SELECT * FROM sessions WHERE session_id='s1'")[0]
    assert row["cost_usd"] == pytest.approx(30.0)
    assert row["cost_source"] == COST_ESTIMATED


def test_a_mixed_model_session_is_priced_per_model(store, projects):
    """Pricing the whole session at one model's rate is the trap claude-usage
    falls into in its own session table."""
    _write(projects, "s1", [
        _assistant(message_id="m1", model="claude-opus-4-8", output_tokens=1_000_000),
        _assistant(message_id="m2", model="claude-haiku-4-5", output_tokens=1_000_000),
    ])
    scan(store, projects)
    cost = store._query("SELECT cost_usd FROM sessions")[0]["cost_usd"]
    assert cost == pytest.approx(25.0 + 5.0)


def test_a_measured_cost_is_never_replaced_by_an_estimate(store, projects):
    """Claude Code's own figure is the one number here that needs no disclaimer."""
    store.upsert_session("s1", cost_usd=1.23, cost_source=COST_MEASURED)
    _write(projects, "s1", [_assistant(model="claude-opus-4-8",
                                       output_tokens=1_000_000)])
    scan(store, projects)
    row = store._query("SELECT * FROM sessions WHERE session_id='s1'")[0]
    assert row["cost_usd"] == pytest.approx(1.23)
    assert row["cost_source"] == COST_MEASURED


def test_an_unpriceable_session_gets_no_cost(store, projects):
    _write(projects, "s1", [_assistant(model="some-local-model", output_tokens=99)])
    scan(store, projects)
    rows = store._query("SELECT cost_usd, cost_source FROM sessions")
    assert not rows or rows[0]["cost_usd"] is None


def test_promotional_rate_applies_by_the_turn_s_own_day(store, projects, monkeypatch):
    # Sonnet 5's own window was dropped when its page stopped printing an end
    # date; a promo row stands in for the next launch price.
    from dark_army_daemon import pricing
    monkeypatch.setitem(pricing.RATES, "claude-test-promo", pricing.Rate(
        3.00, 15.00, intro_input=2.00, intro_output=10.00,
        intro_until="2026-08-31"))
    _write(projects, "s1", [
        _assistant(message_id="m1", model="claude-test-promo",
                   ts="2026-07-26T12:00:00Z", input_tokens=1_000_000),
        _assistant(message_id="m2", model="claude-test-promo",
                   ts="2026-09-05T12:00:00Z", input_tokens=1_000_000),
    ])
    scan(store, projects)
    cost = store._query("SELECT cost_usd FROM sessions")[0]["cost_usd"]
    assert cost == pytest.approx(2.00 + 3.00)


def test_project_comes_from_the_transcript_cwd_not_the_directory_name(store, projects):
    """The directory encodes the cwd with every separator replaced by a dash, so
    `shop-front` and `shop/front` are indistinguishable — splitting on the last
    dash reads the project as "front". The transcript's own `cwd` is exact."""
    record = json.loads(_assistant(output_tokens=10))
    record["cwd"] = "/Users/x/Documents/shop-front"
    _write(projects, "s1", [json.dumps(record)])
    scan(store, projects)
    row = store._query("SELECT project, cwd FROM sessions")[0]
    assert row["project"] == "shop-front"
    assert row["cwd"] == "/Users/x/Documents/shop-front"


def test_directory_name_is_the_fallback_when_cwd_is_absent(store, projects):
    _write(projects, "s1", [_assistant(output_tokens=10)])
    scan(store, projects)
    assert store._query("SELECT project FROM sessions")[0]["project"] == "repo"


# --- compactions -------------------------------------------------------------
#
# Record shape from a real transcript: a `system` record with subtype
# `compact_boundary`, carrying how full the window was and who asked.


def _compact(session="s1", uuid="c1", ts="2026-07-26T12:30:00Z", trigger="auto",
             pre_tokens=167_436, **extra):
    return json.dumps({
        "type": "system",
        "subtype": "compact_boundary",
        "content": "Conversation compacted",
        "sessionId": session,
        "timestamp": ts,
        "uuid": uuid,
        "compactMetadata": {"trigger": trigger, "preTokens": pre_tokens,
                            "durationMs": 3},
        **extra,
    })


def test_compaction_boundary_is_parsed():
    [event] = transcript_scan.parse_compactions([_compact()])
    assert event["trigger"] == "auto"
    assert event["pre_tokens"] == 167_436
    assert event["uuid"] == "c1"


def test_a_manual_compaction_keeps_its_trigger():
    [event] = transcript_scan.parse_compactions([_compact(trigger="manual")])
    assert event["trigger"] == "manual"


def test_other_system_records_are_not_compactions():
    """The prefilter matches the word anywhere in the line, so the subtype is what
    actually decides — a queue-operation mentioning compaction is not one."""
    noise = json.dumps({"type": "system", "subtype": "other",
                        "content": "isCompactSummary compact_boundary",
                        "sessionId": "s1", "timestamp": "2026-07-26T12:00:00Z"})
    assert transcript_scan.parse_compactions([noise]) == []


def test_a_compaction_without_a_session_or_timestamp_is_skipped():
    bad = json.dumps({"type": "system", "subtype": "compact_boundary", "uuid": "c1"})
    assert transcript_scan.parse_compactions([bad]) == []


def test_compaction_metadata_may_be_missing():
    """Absent metadata is not a reason to lose the fact that it happened."""
    bare = json.dumps({"type": "system", "subtype": "compact_boundary",
                       "sessionId": "s1", "timestamp": "2026-07-26T12:00:00Z",
                       "uuid": "c1"})
    [event] = transcript_scan.parse_compactions([bare])
    assert event["trigger"] == "" and event["pre_tokens"] is None


def test_scan_ingests_compactions(store, projects):
    _write(projects, "s1", [_assistant(output_tokens=10), _compact()])
    summary = scan(store, projects)
    assert summary["compactions_added"] == 1
    assert store.context_pressure(days=None)["compactions"] == 1


def test_rescanning_never_duplicates_a_compaction(store, projects):
    path = _write(projects, "s1", [_assistant(output_tokens=10), _compact()])
    scan(store, projects)
    # A new mtime forces the file to be read again; the uuid index is what makes
    # the second read contribute nothing.
    path.touch()
    scan(store, projects)
    assert store.context_pressure(days=None)["compactions"] == 1


# --- titles ------------------------------------------------------------------
#
# The live writer only stores statusline `session_name`, which Claude Code
# leaves empty once terminal_title disables AI title generation. The scan is
# what has the transcript, so it is what has to name the History row.


def test_scan_fills_title_from_ai_title(store, projects):
    _write(projects, "s1", [
        _user(),
        _ai_title(title="Fix the sprite pipeline"),
        _assistant(output_tokens=10),
    ])
    assert scan(store, projects)["titles_filled"] == 1
    assert store._query("SELECT title FROM sessions")[0]["title"] == \
        "Fix the sprite pipeline"


def test_scan_falls_back_to_the_opening_prompt(store, projects):
    _write(projects, "s1", [
        _user(text="fix the sprite pipeline please"),
        _assistant(output_tokens=10),
    ])
    scan(store, projects)
    assert store._query("SELECT title FROM sessions")[0]["title"] == \
        "fix the sprite pipeline please"


def test_scan_does_not_overwrite_an_existing_title(store, projects):
    store.upsert_session("s1", title="Name I chose")
    _write(projects, "s1", [
        _user(),
        _ai_title(title="Something else"),
        _assistant(output_tokens=10),
    ])
    assert scan(store, projects)["titles_filled"] == 0
    assert store._query("SELECT title FROM sessions")[0]["title"] == "Name I chose"


def test_scan_backfills_a_title_on_an_already_ingested_file(store, projects):
    """Unchanged files are skipped for turns. Titles still have to land."""
    _write(projects, "s1", [
        _user(),
        _ai_title(title="Fix the sprite pipeline"),
        _assistant(output_tokens=10),
    ])
    scan(store, projects)
    store._write("UPDATE sessions SET title = NULL WHERE session_id = 's1'", ())
    assert store._query("SELECT title FROM sessions")[0]["title"] is None
    summary = scan(store, projects)
    assert summary["files_read"] == 0
    assert summary["titles_filled"] == 1
    assert store._query("SELECT title FROM sessions")[0]["title"] == \
        "Fix the sprite pipeline"


def test_scan_uses_a_generated_title_when_the_transcript_has_none(
        store, projects, tmp_path, monkeypatch):
    titles = tmp_path / "titles.json"
    titles.write_text(json.dumps({"s1": "Window design for orchestration"}))
    monkeypatch.setattr(transcript_scan, "TITLES_PATH", titles)
    _write(projects, "s1", [
        _user(text="i'm thinking on a way the window actually works"),
        _assistant(output_tokens=10),
    ])
    scan(store, projects)
    assert store._query("SELECT title FROM sessions")[0]["title"] == \
        "Window design for orchestration"


def test_a_transcript_with_only_a_compaction_still_gets_its_project(store, projects):
    """Otherwise the compaction lands with no session row and the report files it
    under "?" — a project that is right there in the record."""
    record = json.loads(_compact())
    record["cwd"] = "/Users/x/Documents/shop-front"
    _write(projects, "s1", [json.dumps(record)])
    scan(store, projects)
    assert store.context_pressure(days=None)["by_project"][0]["project"] == "shop-front"


# --- attribution and subagent transcripts ------------------------------------


def _attributed(**fields):
    """One assistant record with attribution stamps, as Claude Code writes them:
    on the same record that carries the usage."""
    record = json.loads(_assistant(output_tokens=100, **{
        k: v for k, v in fields.items() if k in ("session", "message_id", "ts")}))
    for key in ("attributionSkill", "attributionAgent", "attributionPlugin",
                "attributionMcpServer", "attributionMcpTool"):
        if key in fields:
            record[key] = fields[key]
    if fields.get("sidechain"):
        record["isSidechain"] = True
        record["agentId"] = fields.get("agent_id", "a1")
    return json.dumps(record)


def test_attribution_is_read_off_the_assistant_record():
    [turn] = parse_turns([_attributed(
        attributionSkill="artifact-design", attributionMcpServer="gitnexus",
        attributionMcpTool="impact")])
    assert turn["attr_skill"] == "artifact-design"
    assert turn["attr_mcp_server"] == "gitnexus"
    assert turn["attr_mcp_tool"] == "impact"


def test_absent_attribution_is_none_not_empty_string():
    """The reports group on "column is not null"; an empty string would become a
    bucket whose name is nothing."""
    [turn] = parse_turns([_assistant(output_tokens=5)])
    assert turn["attr_skill"] is None and turn["attr_agent"] is None


@pytest.mark.parametrize("written,expected", [
    ("general-purpose", "general-purpose"),
    ("agent:builtin:Explore", "Explore"),
    ("agent:custom:my-agent", "my-agent"),
])
def test_agent_attribution_loses_its_namespace(written, expected):
    """Bare and namespaced spellings both occur, and one agent must not appear as
    two rows because of it."""
    [turn] = parse_turns([_attributed(attributionAgent=written)])
    assert turn["attr_agent"] == expected


def test_attribution_reaches_the_database(store, projects):
    _write(projects, "s1", [_attributed(attributionSkill="dataviz")])
    scan(store, projects)
    report = store.usage_attribution(days=None)
    assert [(r["name"], r["pct"]) for r in report["skills"]] == [("dataviz", 100)]


def _subagent_file(projects, session, agent_id, records):
    path = projects / "-Users-x-repo" / session / "subagents" / f"agent-{agent_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(records) + "\n")
    return path


def test_subagent_transcripts_are_scanned(store, projects):
    """They live one directory deeper than the session transcript and are not
    copied into it, so a glob that misses them under-counts every total by the
    whole subagent workload."""
    _write(projects, "s1", [_assistant(message_id="m1", output_tokens=10)])
    _subagent_file(projects, "s1", "a1", [_attributed(
        message_id="sub1", attributionAgent="Explore", sidechain=True)])
    summary = scan(store, projects)
    assert summary["turns_added"] == 2
    assert [r["name"] for r in store.usage_attribution(days=None)["agents"]] == ["Explore"]


def test_a_subagent_turn_is_filed_under_its_session_not_its_filename(store, projects):
    """`agent-a1.jsonl` is a file name, not a session id. Keying on the stem files
    every subagent under a session that does not exist — and the parent session
    then never gets the cost."""
    _subagent_file(projects, "s1", "a1", [_attributed(
        session="s1", message_id="sub1", attributionAgent="Explore", sidechain=True)])
    scan(store, projects)
    assert [row["session_id"] for row in store.recent_sessions(days=None)] == ["s1"]


def test_a_subagent_turn_is_marked_as_one(store, projects):
    _subagent_file(projects, "s1", "a1", [_attributed(
        message_id="sub1", attributionAgent="Explore", sidechain=True)])
    scan(store, projects)
    behaviours = {b["key"] for b in store.usage_attribution(days=None)["behaviours"]}
    assert "subagents" in behaviours


def test_a_mid_write_tail_line_is_recorded_once_when_it_completes(store, projects):
    """A transcript caught mid-append used to count its partial last line in the
    stored position; the next scan then skipped the completed line and that
    turn's tokens were lost from history.db permanently. The unterminated tail
    is not consumed — the position stays just before it."""
    import os
    full = _assistant(message_id="m1", output_tokens=1)
    partial = _assistant(message_id="m2", output_tokens=2)
    path = projects / "-Users-x-repo" / "s1.jsonl"
    path.write_text(full + "\n" + partial[:25])

    scan(store, projects)
    assert store._scalar("SELECT COUNT(*) FROM turns") == 1

    with path.open("a") as handle:
        handle.write(partial[25:] + "\n")
    now = time.time()
    os.utime(path, (now + 5, now + 5))

    summary = scan(store, projects)
    assert summary["turns_added"] == 1, "the completed line was skipped"
    assert store._scalar("SELECT COUNT(*) FROM turns") == 2
    assert store._scalar("SELECT SUM(output_tokens) FROM turns") == 3

    # And exactly once: a third scan adds nothing.
    os.utime(path, (now + 10, now + 10))
    assert scan(store, projects)["turns_added"] == 0


# --- the shared record iterator ------------------------------------------------


def test_iter_records_is_the_one_prefilter_parse_typecheck_loop():
    """parse_turns, parse_compactions, read_ai_title and _first_user_prompt
    each re-implemented this loop; they now all route here."""
    from dark_army_daemon.transcript_scan import iter_records

    lines = [
        '{"type": "assistant", "usage": 1}',
        'not json but mentions "assistant"',        # malformed: skipped
        '["assistant"]',                            # not a dict: skipped
        '{"type": "user", "usage": 1}',             # wrong type: skipped
        '{"type": "assistant", "n": 2}',            # no marker: never parsed
        '{"type": "assistant", "usage": 3}',
    ]
    got = list(iter_records(lines, '"usage"', "assistant"))
    assert [r.get("usage") for r in got] == [1, 3]

    # The type field is a parameter: compaction records key on `subtype`.
    boundary = ['{"type": "system", "subtype": "compact_boundary", "m": 1}']
    assert list(iter_records(boundary, "compact_boundary",
                             "compact_boundary", type_field="subtype")) == [
        {"type": "system", "subtype": "compact_boundary", "m": 1}]

    # A torn trailing line of a live file fails the parse and is skipped; one
    # that parses whole is a finished record still waiting for its newline.
    torn = ['{"type": "assistant", "usage": 9}', '{"type": "assistant", "usa']
    assert [r["usage"] for r in iter_records(torn, '"usa', "assistant")] == [9]


# --- the title-miss memo --------------------------------------------------------


def test_a_titleless_transcript_is_not_restreamed_every_pass(
        store, projects, monkeypatch):
    """This pass runs every ~15 minutes forever, and `_first_user_prompt` is an
    uncached whole-file stream — a session that will never have a title must
    not cost a full read per pass for the life of the daemon."""
    import os

    import dark_army_daemon.ai_title as ai_title

    path = _write(projects, "s1", [
        _user(text="<system-reminder>never a title"),
        _assistant(output_tokens=10),
    ])
    scan(store, projects)                      # ingests; the title stays empty
    assert store._query("SELECT title FROM sessions")[0]["title"] is None

    calls = []
    real = ai_title.history_title

    def _counting(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(ai_title, "history_title", _counting)
    scan(store, projects)
    assert calls == [], "an unchanged miss was re-streamed"

    # A changed transcript invalidates the memo: the answer can now differ.
    with path.open("a") as fh:
        fh.write(_ai_title(title="Named at last") + "\n")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    summary = scan(store, projects)
    assert len(calls) == 1
    assert summary["titles_filled"] == 1
    assert store._query("SELECT title FROM sessions")[0]["title"] == \
        "Named at last"


# --- the cache-write split reaches the database -------------------------------


def _split_record(message_id="m1", five=100, hour=200, total=300):
    return json.dumps({
        "type": "assistant", "sessionId": "s1", "timestamp": "2026-07-26T12:00:00Z",
        "message": {"id": message_id, "model": "claude-opus-4-8", "usage": {
            "output_tokens": 5,
            "cache_creation_input_tokens": total,
            "cache_creation": {"ephemeral_5m_input_tokens": five,
                               "ephemeral_1h_input_tokens": hour},
        }},
    })


def _split_of(store, message_id="m1"):
    row = store._query("SELECT cache_write_5m, cache_write_1h FROM turns"
                       " WHERE message_id = ?", (message_id,))[0]
    return row["cache_write_5m"], row["cache_write_1h"]


def test_a_record_with_both_ttl_counts_stores_both_columns(store, projects):
    _write(projects, "s1", [_split_record()])
    scan(store, projects)
    assert _split_of(store) == (100, 200)


def test_a_stated_zero_split_is_stored_as_zero(store, projects):
    _write(projects, "s1", [_split_record(five=0, hour=0, total=0)])
    scan(store, projects)
    assert _split_of(store) == (0, 0)


def test_a_total_only_record_leaves_the_split_null(store, projects):
    """NULL is "the record did not say"; a 5-minute share would be invented."""
    _write(projects, "s1", [_assistant(message_id="m1", output_tokens=5,
                                       cache_creation=500)])
    scan(store, projects)
    assert _split_of(store) == (None, None)
    row = store._query("SELECT cache_creation FROM turns")[0]
    assert row["cache_creation"] == 500


def test_rescanning_the_same_message_fills_the_split_and_keeps_its_cost(store, projects):
    """What the v7 upgrade relies on: an older build stored the turn without
    the split, the forgotten position makes the scanner read it again, and
    only the two columns change."""
    store.add_turn("s1", time.time(), message_id="m1", model="claude-opus-4-8",
                   output_tokens=5, cache_creation=300, cost_usd=0.42,
                   provider="claude")
    assert _split_of(store) == (None, None)
    path = _write(projects, "s1", [_split_record()])
    first = scan(store, projects)
    assert first["turns_added"] == 0
    assert _split_of(store) == (100, 200)
    row = store._query("SELECT cost_usd, cache_creation FROM turns")[0]
    assert row["cost_usd"] == pytest.approx(0.42)
    assert row["cache_creation"] == 300
    assert store._scalar("SELECT COUNT(*) FROM turns") == 1
    # And a re-read with the position forgotten again is still one row.
    store.set_scan_position(str(path), 0.0, 0)
    scan(store, projects)
    assert store._scalar("SELECT COUNT(*) FROM turns") == 1
