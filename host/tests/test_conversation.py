"""The conversation adapter, pager and sealed read."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

from dark_army_daemon import conversation
from dark_army_daemon import session_stats
from dark_army_daemon.api_server import ApiServer, _Request
from dark_army_daemon.conversation import (
    KINDS,
    NO_TRANSCRIPT_REASON,
    PAGE_BYTES,
    PAGE_TURNS,
    RESULT_SUMMARY_CHARS,
    TOOL_BRIEF_CHARS,
    TURN_TEXT_CHARS,
    UNKNOWN_SESSION_REFUSAL,
    ConversationReader,
    Turn,
    clamp_text,
    fold_claude,
    fold_codex,
    fold_grok,
    result_summary,
    tool_brief,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "conversation"
PHONE = ROOT / "ios" / "BobPhone"
CLIENT = PHONE / "Client.swift"
APP = ROOT / "ios" / "BobPhone" / "BobPhoneApp.swift"
MODELS = PHONE / "Models.swift"
DETAIL = PHONE / "AgentDetailView.swift"
CONV_VIEW = PHONE / "ConversationView.swift"
CONV_MODELS = PHONE / "ConversationModels.swift"
AGENT_SCREEN = PHONE / "AgentScreen.swift"

EXPECTED_KINDS = {
    "claude": ["user", "agent", "tool", "result", "agent"],
    "codex": ["user", "agent", "tool", "result", "tool", "result", "note"],
    "grok": ["user", "agent", "tool", "result", "result", "agent"],
}


def _page(provider: str, path: Path, since: int = 0, key: str = "") -> dict:
    return ConversationReader().page(provider, str(path), since, key)


def _turns(provider: str, path: Path) -> list[dict]:
    page = _page(provider, path)
    assert page["available"] is True
    return page["turns"]


# --- 1–10: adapter and pager -------------------------------------------------


@pytest.mark.parametrize("provider", ("claude", "codex", "grok"))
def test_each_provider_folds_to_the_one_shape(provider):
    keys = set(Turn(0, "user", 0.0).to_dict())
    turns = _turns(provider, FIXTURES / f"{provider}.jsonl")
    assert [row["kind"] for row in turns] == EXPECTED_KINDS[provider]
    for i, row in enumerate(turns):
        assert set(row) == keys
        assert row["kind"] in KINDS
        assert row["seq"] == i


def test_synthetic_prompts_and_reasoning_never_become_turns():
    acc = conversation._Accum()
    fold_claude({
        "type": "user",
        "timestamp": "2026-09-21T10:00:00Z",
        "message": {"content": [{"type": "text", "text": "<system-reminder>x"}]},
    }, acc)
    fold_claude({
        "type": "assistant",
        "timestamp": "2026-09-21T10:00:01Z",
        "message": {
            "model": "claude-opus-4-1",
            "content": [{"type": "thinking", "thinking": "secret"}],
        },
    }, acc)
    fold_codex({
        "timestamp": "2026-09-21T11:00:00Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text",
                         "text": "# AGENTS.md instructions for x\nHi"}],
        },
    }, acc)
    fold_codex({
        "timestamp": "2026-09-21T11:00:01Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "Be concise."}],
        },
    }, acc)
    fold_codex({
        "timestamp": "2026-09-21T11:00:02Z",
        "type": "response_item",
        "payload": {"type": "reasoning", "encrypted": True},
    }, acc)
    fold_grok({"type": "user", "content": "<user_info>OS: macos</user_info>"}, acc)
    fold_grok({"type": "reasoning", "content": "thinking"}, acc)
    assert acc.turns == []


def test_tool_turns_carry_a_brief_and_results_carry_a_summary_never_the_bytes():
    blob = "x" * 40_000
    acc = conversation._Accum()
    fold_claude({
        "type": "user",
        "timestamp": "2026-09-21T10:00:00Z",
        "message": {"content": [{
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": blob,
        }]},
    }, acc)
    assert len(acc.turns) == 1
    row = acc.turns[0].to_dict()
    assert row["kind"] == "result"
    assert row["result_bytes"] == 40_000
    assert len(row["text"]) <= RESULT_SUMMARY_CHARS
    assert blob not in row["text"]
    assert tool_brief("Bash", {"command": "ls -la"}) == "ls -la"
    assert tool_brief("Read", {"file_path": "a.py"}) == "a.py"
    assert tool_brief("Mystery", {"other": "first string"}) == "first string"
    assert len(tool_brief("Bash", {"command": "c" * 400})) == TOOL_BRIEF_CHARS


def test_grok_turns_carry_no_time_and_say_so():
    grok = _turns("grok", FIXTURES / "grok.jsonl")
    assert grok and all(row["ts"] == 0.0 for row in grok)
    claude = _turns("claude", FIXTURES / "claude.jsonl")
    codex = _turns("codex", FIXTURES / "codex.jsonl")
    assert claude and all(row["ts"] > 0 for row in claude)
    assert codex and all(row["ts"] > 0 for row in codex)


def test_markers_are_stripped_from_agent_text():
    acc = conversation._Accum()
    fold_claude({
        "type": "assistant",
        "timestamp": "2026-09-21T10:00:00Z",
        "message": {
            "model": "claude-opus-4-1",
            "content": [{"type": "text", "text":
                         "Hello\n<!-- bob-tldr: secret -->\n<!-- bob-actions: A | B -->"}],
        },
    }, acc)
    assert acc.turns
    text = acc.turns[0].text
    assert "bob-tldr" not in text
    assert "bob-actions" not in text
    assert "Hello" in text


def test_a_long_turn_is_clamped_and_says_so():
    text, truncated = clamp_text("z" * 20_000)
    assert truncated is True
    assert len(text) == TURN_TEXT_CHARS
    acc = conversation._Accum()
    fold_claude({
        "type": "user",
        "timestamp": "2026-09-21T10:00:00Z",
        "message": {"content": "z" * 20_000},
    }, acc)
    row = acc.turns[0].to_dict()
    assert row["truncated"] is True
    assert len(row["text"]) == TURN_TEXT_CHARS


def test_the_page_is_bounded_by_turns_and_bytes_and_states_more(tmp_path):
    tiny = tmp_path / "tiny.jsonl"
    lines = [
        json.dumps({
            "type": "user",
            "timestamp": f"2026-09-21T10:00:{i % 60:02d}.000Z",
            "message": {"content": f"u{i}"},
        })
        for i in range(400)
    ]
    tiny.write_text("\n".join(lines) + "\n")
    reader = ConversationReader()
    first = reader.page("claude", str(tiny), 0, "")
    assert first["available"] is True
    assert len(first["turns"]) == PAGE_TURNS
    assert first["more"] is True
    assert first["next_seq"] == PAGE_TURNS
    seen = 0
    since = 0
    key = first["key"]
    hops = 0
    while True:
        page = reader.page("claude", str(tiny), since, key)
        seen += len(page["turns"])
        since = page["next_seq"]
        key = page["key"]
        hops += 1
        if not page["more"]:
            break
    assert seen == first["total"] == 400
    assert hops == 3

    fat = tmp_path / "fat.jsonl"
    fat_lines = [
        json.dumps({
            "type": "user",
            "timestamp": "2026-09-21T10:00:00.000Z",
            "message": {"content": "w" * 5_000},
        })
        for _ in range(100)
    ]
    fat.write_text("\n".join(fat_lines) + "\n")
    page = ConversationReader().page("claude", str(fat), 0, "")
    blob = json.dumps(page, allow_nan=False)
    assert len(blob) <= PAGE_BYTES
    assert page["more"] is True
    assert page["turns"]


def test_a_page_of_oversize_turns_serialises_each_turn_once(tmp_path, monkeypatch):
    """The byte bound used to be met by dropping a turn and re-serialising
    the whole page — 0.65 s per page on a transcript of long turns. One
    pass now: every turn's length once, the envelope once, one final
    dump. The page itself is what the old loop produced."""
    from dark_army_daemon import conversation as module
    fat = tmp_path / "fat.jsonl"
    fat.write_text("\n".join(
        json.dumps({"type": "user",
                    "timestamp": "2026-09-21T10:00:00.000Z",
                    "message": {"content": "w" * 5_000}})
        for _ in range(100)) + "\n")
    reader = ConversationReader()
    real = module.json

    class Counting:
        calls = 0

        def __getattr__(self, name):
            return getattr(real, name)

        def dumps(self, *a, **kw):
            Counting.calls += 1
            return real.dumps(*a, **kw)

    monkeypatch.setattr(module, "json", Counting())
    page = reader.page("claude", str(fat), 0, "")
    blob = json.dumps(page, allow_nan=False)
    assert len(blob) <= PAGE_BYTES
    # What the old loop kept: the longest prefix of the window that fits.
    kept = len(page["turns"])
    assert 0 < kept < 100
    # The envelope once, each kept turn once, and the one that overflowed.
    assert Counting.calls == kept + 2
    longer = dict(page, turns=page["turns"] + [page["turns"][0]])
    assert len(json.dumps(longer, allow_nan=False)) > PAGE_BYTES
    assert page["next_seq"] == kept and page["more"] is True
    assert page["total"] == 100
    # The cursor keeps walking off the page's own `next_seq`.
    rest = reader.page("claude", str(fat), page["next_seq"], page["key"])
    assert rest["turns"][0]["seq"] == kept


def test_the_cursor_is_a_slice_and_a_changed_key_resets(tmp_path):
    path = tmp_path / "cursor.jsonl"
    lines = [
        json.dumps({
            "type": "user",
            "timestamp": "2026-09-21T10:00:00.000Z",
            "message": {"content": f"u{i}"},
        })
        for i in range(20)
    ]
    path.write_text("\n".join(lines) + "\n")
    reader = ConversationReader()
    whole = reader.page("claude", str(path), 0, "")
    assert whole["total"] == 20
    keyed = reader.page("claude", str(path), 10, whole["key"])
    assert keyed["reset"] is False
    assert keyed["turns"][0]["seq"] == 10
    other = reader.page("claude", str(path), 10, "other")
    assert other["reset"] is True
    assert other["turns"][0]["seq"] == 0
    past = reader.page("claude", str(path), whole["total"] + 5, whole["key"])
    assert past["reset"] is True
    assert past["turns"][0]["seq"] == 0


def test_a_grown_file_is_read_from_the_delta(tmp_path):
    src = FIXTURES / "claude.jsonl"
    dest = tmp_path / "claude.jsonl"
    dest.write_bytes(src.read_bytes())
    if not dest.read_bytes().endswith(b"\n"):
        dest.write_bytes(dest.read_bytes() + b"\n")
    reader = ConversationReader()
    first = reader.page("claude", str(dest), 0, "")
    folded_first = reader._caches["claude"].last_folded
    assert folded_first > 0
    extra = json.dumps({
        "type": "user",
        "timestamp": "2026-09-21T10:09:00.000Z",
        "message": {"content": "and then this"},
    }) + "\n"
    with dest.open("ab") as fh:
        fh.write(extra.encode())
    second = reader.page("claude", str(dest), first["next_seq"], first["key"])
    assert reader._caches["claude"].last_folded == 1
    assert second["turns"]
    assert second["turns"][-1]["text"] == "and then this"


def test_a_shrunk_file_is_reread_whole(tmp_path):
    src = FIXTURES / "claude.jsonl"
    dest = tmp_path / "claude.jsonl"
    dest.write_bytes(src.read_bytes())
    reader = ConversationReader()
    first = reader.page("claude", str(dest), 0, "")
    dest.unlink()
    dest.write_text(json.dumps({
        "type": "user",
        "timestamp": "2026-09-21T12:00:00.000Z",
        "message": {"content": "fresh"},
    }) + "\n")
    second = reader.page("claude", str(dest), 0, first["key"])
    assert second["reset"] is True
    assert second["key"] != first["key"]
    assert second["turns"][0]["text"] == "fresh"


# --- A reply from the panel or the phone is the person's turn ---------------

_DEFAULT = object()


def _channel_record(ts, text, *, source="bob", kind="user", as_blocks=False,
                    origin=_DEFAULT, meta=True, sidechain=False):
    """The record `reply_to_session` leaves in a Claude transcript, in the
    shape found on this Mac: `isMeta`, `origin.kind == "channel"`, the words
    wrapped in the channel's tags."""
    body = f'<channel source="{source}" kind="{kind}">\n{text}\n</channel>'
    record = {
        "type": "user",
        "timestamp": ts,
        "isMeta": meta,
        "isSidechain": sidechain,
        "origin": {"kind": "channel", "server": source},
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": body}] if as_blocks else body,
        },
    }
    if origin is None:
        del record["origin"]
    elif origin is not _DEFAULT:
        record["origin"] = origin
    return record


def _agent_record(ts, text):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-1",
            "content": [{"type": "text", "text": text}],
        },
    }


def _fold(*records) -> list:
    acc = conversation._Accum()
    for record in records:
        fold_claude(record, acc)
    return acc.turns


def test_a_panel_reply_is_the_persons_turn_with_the_tag_stripped():
    turns = _fold(_channel_record("2026-09-06T15:52:44.883Z", "Accept"))
    assert len(turns) == 1
    turn = turns[0]
    assert turn.kind == "user"
    assert turn.text == "Accept"
    assert turn.ts > 0
    assert "<channel" not in turn.text
    assert "</channel>" not in turn.text


def test_a_reply_under_the_new_channel_name_counts_too():
    turns = _fold(_channel_record(
        "2026-09-06T15:52:44.883Z", "Accept", source="dark-army",
    ))
    assert [(t.kind, t.text) for t in turns] == [("user", "Accept")]


def test_a_reply_as_blocks_counts():
    turns = _fold(_channel_record(
        "2026-09-06T15:52:44.883Z", "Accept", as_blocks=True,
    ))
    assert [(t.kind, t.text) for t in turns] == [("user", "Accept")]


def test_a_reply_keeps_its_own_lines_and_inner_markup():
    words = "Do this:\n1. a\n2. b\n</channel> is not the end"
    turns = _fold(_channel_record("2026-09-06T15:52:44.883Z", words))
    assert [(t.kind, t.text) for t in turns] == [("user", words)]


@pytest.mark.parametrize("words", ("", "   \n"))
def test_an_empty_reply_pushes_nothing(words):
    assert _fold(_channel_record("2026-09-06T15:52:44.883Z", words)) == []


def test_a_reply_that_is_only_the_two_tags_pushes_nothing():
    record = _channel_record("2026-09-06T15:52:44.883Z", "")
    record["message"]["content"] = '<channel source="bob" kind="user"></channel>'
    assert _fold(record) == []


def test_a_fleet_event_stays_hidden():
    assert _fold(_channel_record(
        "2026-09-06T15:52:44.883Z", "Peter finished", kind="fleet",
    )) == []


def _skill_expansion():
    record = _channel_record("2026-09-06T15:52:44.883Z", "x", origin=None)
    record["message"]["content"] = [{
        "type": "text",
        "text": (
            "Base directory for this skill: /x\n\nWhen a message arrives as "
            '<channel source="bob" kind="user">\nAccept\n</channel> treat it '
            "as the person."
        ),
    }]
    return record


def _peer_hand_back():
    record = _channel_record(
        "2026-09-06T15:52:44.883Z", "x", origin={"kind": "peer", "from": "x"},
    )
    record["message"]["content"] = (
        "Another Claude session sent a message:\n"
        '<channel source="bob" kind="user">\nAccept\n</channel>'
    )
    return record


def _local_command_caveat():
    return {
        "type": "user",
        "timestamp": "2026-09-06T15:52:44.883Z",
        "isMeta": True,
        "message": {
            "role": "user",
            "content": "<local-command-caveat>Caveat: generated.</local-command-caveat>",
        },
    }


@pytest.mark.parametrize("record", (
    pytest.param(_skill_expansion(), id="skill-expansion-no-origin"),
    pytest.param(_peer_hand_back(), id="peer-hand-back"),
    pytest.param(_channel_record(
        "2026-09-06T15:52:44.883Z", "Accept", origin={"kind": "other"},
    ), id="other-origin"),
    pytest.param(_channel_record(
        "2026-09-06T15:52:44.883Z", "Accept", origin="channel",
    ), id="origin-not-a-dict"),
    pytest.param(_channel_record(
        "2026-09-06T15:52:44.883Z", "Accept", origin=None,
    ), id="tag-without-origin"),
    pytest.param(_channel_record(
        "2026-09-06T15:52:44.883Z", "Accept", sidechain=True,
    ), id="sidechain"),
    pytest.param(_local_command_caveat(), id="local-command-caveat"),
))
def test_the_real_look_alikes_stay_hidden(record):
    assert _fold(record) == []


def test_a_channel_record_without_the_user_tag_stays_hidden():
    record = _channel_record("2026-09-06T15:52:44.883Z", "x")
    record["message"]["content"] = "hello"
    assert _fold(record) == []


def test_the_reading_is_session_stats_own():
    assert conversation._is_channel_user_message is session_stats._is_channel_user_message
    assert conversation._CHANNEL_USER_RE is session_stats._CHANNEL_USER_RE
    real = '<channel source="bob" kind="user">\nAccept\n</channel>'
    assert conversation.channel_reply_text(real) == "Accept"
    assert conversation.channel_reply_text(
        '<channel source="dark-army" kind="user">\nAccept\n</channel>\n'
    ) == "Accept"
    assert conversation.channel_reply_text(
        '<channel source="bob" kind="user">\nno close'
    ) == "no close"
    assert conversation.channel_reply_text("Accept") == ""
    assert conversation.channel_reply_text(
        'Before <channel source="bob" kind="user">\nAccept\n</channel>'
    ) == ""
    assert conversation.channel_reply_text(
        '<channel source="bob" kind="fleet">\nAccept\n</channel>'
    ) == ""


def _write_jsonl(path: Path, *records) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def test_a_reply_lands_between_the_agents_turns_in_order(tmp_path):
    path = tmp_path / "claude.jsonl"
    _write_jsonl(
        path,
        _agent_record("2026-09-21T10:00:00.000Z", "Shall I merge?"),
        _channel_record("2026-09-21T10:01:00.000Z", "Yes, merge it"),
        _agent_record("2026-09-21T10:02:00.000Z", "Merged."),
    )
    turns = _page("claude", path)["turns"]
    assert [row["kind"] for row in turns] == ["agent", "user", "agent"]
    assert turns[1]["text"] == "Yes, merge it"
    assert [row["seq"] for row in turns] == [0, 1, 2]
    assert turns[0]["ts"] < turns[1]["ts"] < turns[2]["ts"]


def test_a_panel_reply_appended_later_is_read_from_the_delta(tmp_path):
    src = FIXTURES / "claude.jsonl"
    dest = tmp_path / "claude.jsonl"
    dest.write_bytes(src.read_bytes())
    if not dest.read_bytes().endswith(b"\n"):
        dest.write_bytes(dest.read_bytes() + b"\n")
    reader = ConversationReader()
    first = reader.page("claude", str(dest), 0, "")
    assert reader._caches["claude"].last_folded > 0
    _write_jsonl(
        dest,
        _channel_record("2026-09-21T10:09:00.000Z", "Yes, go on"),
        _agent_record("2026-09-21T10:10:00.000Z", "Going on."),
    )
    second = reader.page("claude", str(dest), first["next_seq"], first["key"])
    assert reader._caches["claude"].last_folded == 2
    assert [row["kind"] for row in second["turns"]] == ["user", "agent"]
    assert second["turns"][0]["text"] == "Yes, go on"
    fresh = ConversationReader().page("claude", str(dest), 0, "")["turns"]
    assert fresh == first["turns"] + second["turns"]


# --- 11–12, 19: daemon source and marker (filled in with later steps) --------


@pytest.mark.asyncio
async def test_an_unknown_session_is_refused_without_touching_disk():
    from dark_army_daemon.daemon import BobDaemon

    class Stub:
        def __init__(self):
            self._conversations = ConversationReader()
            self.page_calls = 0

            def spy(*args, **kwargs):
                self.page_calls += 1
                return ConversationReader.page(
                    self._conversations, *args, **kwargs)

            self._conversations.page = spy  # type: ignore[method-assign]

        def _inbox_session_entry(self, sid):
            return None, False

        _session_states: dict = {}
        _codex_records: dict = {}

    stub = Stub()
    stub.conversation_source = BobDaemon.conversation_source.__get__(stub)
    stub.conversation_page = BobDaemon.conversation_page.__get__(stub)
    out = await stub.conversation_page("missing", 0, "")
    assert out["available"] is False
    assert out["reason"] == UNKNOWN_SESSION_REFUSAL
    assert stub.page_calls == 0


@pytest.mark.asyncio
async def test_the_source_is_resolved_per_provider(tmp_path, monkeypatch):
    from dark_army_daemon import grok_roster
    from dark_army_daemon.codex_rollouts import CodexRecord
    from dark_army_daemon.daemon import BobDaemon

    grok_root = tmp_path / "grok"
    cwd = str(tmp_path / "proj")
    monkeypatch.setattr(grok_roster, "SESSIONS_DIR", grok_root)
    grok_dir = grok_roster.session_dir("g1", cwd, sessions_dir=grok_root)
    # session_dir returns None until the directory exists.
    encoded = grok_root / quote(cwd, safe="") / "g1"
    encoded.mkdir(parents=True)
    (encoded / "chat_history.jsonl").write_text(
        json.dumps({"type": "user",
                    "content": "<user_query>hi from grok</user_query>"}) + "\n")

    claude_path = tmp_path / "claude.jsonl"
    shutil.copy(FIXTURES / "claude.jsonl", claude_path)
    codex_path = tmp_path / "codex.jsonl"
    shutil.copy(FIXTURES / "codex.jsonl", codex_path)

    class Stub:
        def __init__(self):
            self._conversations = ConversationReader()
            self._session_states = {
                "c1": {"transcript_path": str(claude_path)},
            }
            self._codex_records = {
                "x1": CodexRecord(session_id="x1", thread_id="t",
                                  path=codex_path),
            }
            self.rows = {
                "g1": {"provider": "grok", "cwd": cwd, "session_id": "g1"},
                "c1": {"provider": "claude", "cwd": cwd, "session_id": "c1"},
                "x1": {"provider": "codex", "cwd": cwd, "session_id": "x1"},
            }

        def _inbox_session_entry(self, sid):
            row = self.rows.get(sid)
            return (row, False) if row else (None, False)

    stub = Stub()
    stub.conversation_source = BobDaemon.conversation_source.__get__(stub)
    stub.conversation_page = BobDaemon.conversation_page.__get__(stub)

    provider, path, sid = stub.conversation_source("g1")
    assert provider == "grok" and sid == "g1"
    assert path.endswith("chat_history.jsonl")
    grok_page = await stub.conversation_page("g1", 0, "")
    assert grok_page["available"] is True
    assert grok_page["turns"][0]["kind"] == "user"

    provider, path, sid = stub.conversation_source("c1")
    assert provider == "claude" and path == str(claude_path)
    claude_page = await stub.conversation_page("c1", 0, "")
    assert claude_page["available"] is True

    provider, path, sid = stub.conversation_source("x1")
    assert provider == "codex" and path == str(codex_path)
    codex_page = await stub.conversation_page("x1", 0, "")
    assert codex_page["available"] is True


def test_the_read_is_a_read_not_an_action():
    assert "conversation" not in ApiServer.LAN_ACTIONS
    assert "conversation" not in ApiServer.REMOTE_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


def _run(coro):
    return asyncio.run(coro)


def _body(result):
    status, ctype, raw = result
    return status, json.loads(raw)


class _Daemon:
    def __init__(self, page=None, refuse=False):
        self._page = page or {
            "available": True,
            "provider": "claude",
            "key": "1:0",
            "reset": False,
            "turns": [],
            "next_seq": 0,
            "total": 0,
            "more": False,
            "generated_at": 1.0,
        }
        self.refuse = refuse
        self.calls = []

    async def conversation_page(self, session, since, key):
        self.calls.append((session, since, key))
        if self.refuse:
            return {"available": False, "reason": UNKNOWN_SESSION_REFUSAL}
        return dict(self._page)


def test_both_sealed_doors_answer_the_kind_with_no_lease_and_no_record():
    srv = ApiServer(_Daemon(), port=0)
    for actions, check_lease, record in (
            (ApiServer.LAN_ACTIONS, False, False),
            (ApiServer.REMOTE_ACTIONS, True, True)):
        status, out = _body(_run(srv._sealed_run(
            "conversation", {"query": "session=s1"}, "dev-1",
            actions=actions, check_lease=check_lease, record=record)))
        assert status == 200
        assert out["available"] is True


def test_home_allowlist_names_the_kind():
    src = inspect.getsource(ApiServer._lan_home)
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    src = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert '"conversation"' in src


def test_bad_queries_are_400_in_words():
    srv = ApiServer(_Daemon(), port=0)
    cases = [
        "",
        "session=" + "s" * 201,
        "session=s1&since=-1",
        "session=s1&since=abc",
        "session=s1&session=s2",
        "session=s1&key=" + "k" * 65,
    ]
    for query in cases:
        status, out = _body(_run(srv._conversation_report_for(query)))
        assert status == 400, query
        assert "error" in out


def test_the_loopback_get_is_token_gated():
    daemon = _Daemon()
    api = ApiServer(daemon, port=0)
    api.token = "token"

    async def _get(headers):
        lines = ["GET /api/conversation?session=x HTTP/1.1"]
        for key, value in headers.items():
            lines.append(f"{key}: {value}")
        lines.append("")
        lines.append("")
        raw = "\r\n".join(lines).encode()
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        reader.feed_eof()

        class _Writer:
            def __init__(self):
                self.buf = bytearray()

            def write(self, data):
                self.buf.extend(data)

            async def drain(self):
                return None

            def close(self):
                return None

            async def wait_closed(self):
                return None

            def get_extra_info(self, name, default=None):
                return default

        writer = _Writer()
        await api._handle_client(reader, writer)
        head, _, body = bytes(writer.buf).partition(b"\r\n\r\n")
        status = int(head.split()[1])
        return status, body

    status, body = _run(_get({"host": "localhost"}))
    assert status == 403
    status, body = _run(_get({
        "host": "localhost", "authorization": "Bearer token"}))
    assert status == 403
    status, body = _run(_get({
        "host": "localhost", "x-bob-token": "token"}))
    assert status in (200, 404)
    payload = json.loads(body)
    assert "error" in payload or payload.get("available") in (True, False)


def test_no_path_rides_the_page():
    for provider in ("claude", "codex", "grok"):
        path = FIXTURES / f"{provider}.jsonl"
        page = _page(provider, path)
        blob = json.dumps(page)
        assert str(path) not in blob
        assert "/Users/" not in blob


def test_the_marker_rides_pipeline_writable():
    from dark_army_daemon.daemon_board import BoardVerbsMixin

    class Stub(BoardVerbsMixin):
        def _observers_implementing(self, name):
            return False

        def _board_projects(self):
            return []

    assert Stub()._pipeline_writable()["conversation_supported"] is True


def test_phone_structure_pins():
    client = CLIENT.read_text()
    poll_direct = client[client.index("private func pollDirect("):
                         client.index("private func pollViaRelay(")]
    poll_relay = client[client.index("private func pollViaRelay("):
                        client.index("private static func relayLeg(")]
    bg = client[client.index("func backgroundRefresh("):
                client.index("func backgroundRefresh(") + 2500]
    assert "fetchConversation(" in poll_direct
    assert "fetchConversation(" in poll_relay
    assert "onversation" not in bg
    assert "conversationCache.adopt(record.token)" in client
    assert "conversationCache.forget()" in client
    assert "client.conversationCache.load()" in APP.read_text()
    view = CONV_VIEW.read_text()
    # Two halves of one box (26 Sep 2026): the answer buttons scroll with
    # the turns, the message composer is pinned beneath the page.
    assert view.count("AnswerBox(") == 2
    assert "part: .choices" in view and "part: .composer" in view
    assert "MarkdownText(" in view
    assert ".lineLimit(" not in view
    assert "ProgressView" not in view
    detail = DETAIL.read_text()
    assert detail.count("DetailTab.terminalAttached(") == 2
    assert "PhoneDetailTabBar" not in detail
    models = MODELS.read_text()
    assert "conversationSupported = c.value(.conversationSupported, false)" in models
    decoded = CONV_MODELS.read_text()
    assert decoded.count("c.value(") >= 9


def test_phone_404_latches_only_on_an_older_mac():
    """A 404 from a Mac that carries `conversation_supported` is this one
    session's "not watching" (`UNKNOWN_SESSION_REFUSAL`), not a missing
    kind. Latching `conversationUnsupported` on it blanked the Conversation
    screen for every agent until the next pairing: the phone never asked
    again. The latch is taken only when the snapshot carries no marker, and
    the per-session 404 lands as that session's own reason."""
    client = CLIENT.read_text()
    fetch = client[client.index("func fetchConversation("):
                   client.index("func catchUpConversation(")]
    branch = fetch[fetch.index("if answer.status == 404 {"):]
    branch = branch[:branch.index("guard answer.failure.isEmpty")]
    assert "if !snapshot.board.conversationSupported {" in branch
    assert branch.index("conversationSupported") \
        < branch.index("conversationUnsupported = true")
    assert branch.count("conversationUnsupported = true") == 1
    assert "conversationUnavailable[sid] = reason" in branch
    assert UNKNOWN_SESSION_REFUSAL in branch


def test_agent_screen_rules_run_under_swiftc(tmp_path):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    detail_src = DETAIL.read_text()
    start = detail_src.index("enum DetailTab: String, CaseIterable {")
    depth = 0
    for i, ch in enumerate(detail_src[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                detail_enum = detail_src[start:i + 1]
                break
    else:
        raise AssertionError("DetailTab enum not found")
    harness = r'''
import Foundation
struct In: Decodable {
    let hosted: Bool
    let screen: String
    let supported: Bool
}
let input = try! JSONDecoder().decode(In.self, from: FileHandle.standardInput.readDataToEndOfFile())
let screen = AgentScreen(rawValue: input.screen) ?? .details
var out: [String: Any] = [:]
out["chipsUnhosted"] = AgentScreen.chips(hosted: false).map { $0.rawValue }
out["chipsHosted"] = AgentScreen.chips(hosted: true).map { $0.rawValue }
out["pane"] = AgentScreen.pane(hosted: input.hosted, screen: screen).rawValue
out["detailTab"] = AgentScreen.detailTab(screen).rawValue
out["defaultOn"] = AgentScreen.defaultScreen(supported: true).rawValue
out["defaultOff"] = AgentScreen.defaultScreen(supported: false).rawValue
let data = try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
'''
    path = tmp_path / "AgentScreenProbe.swift"
    path.write_text("import Foundation\n" + detail_enum + "\n"
                    + AGENT_SCREEN.read_text() + "\n" + harness)
    executable = tmp_path / "AgentScreenProbe"
    built = subprocess.run(
        [swiftc, str(path), "-o", str(executable)],
        capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    ran = subprocess.run(
        [str(executable)],
        input=json.dumps({"hosted": False, "screen": "Terminal",
                          "supported": True}),
        capture_output=True, text=True, timeout=30)
    assert ran.returncode == 0, ran.stderr
    out = json.loads(ran.stdout)
    assert out["chipsUnhosted"] == ["Main", "Conversation", "Details"]
    assert out["chipsHosted"][-1] == "Terminal"
    assert out["pane"] == "Details"
    tab_term = subprocess.run(
        [str(executable)],
        input=json.dumps({"hosted": True, "screen": "Terminal",
                          "supported": True}),
        capture_output=True, text=True, timeout=30)
    tab = json.loads(tab_term.stdout)
    assert tab["detailTab"] == "Terminal"
    tab_conv = subprocess.run(
        [str(executable)],
        input=json.dumps({"hosted": True, "screen": "Conversation",
                          "supported": True}),
        capture_output=True, text=True, timeout=30)
    conv = json.loads(tab_conv.stdout)
    assert conv["detailTab"] == "Details"
    assert out["defaultOn"] == "Main"
    assert out["defaultOff"] == "Main"


# --- a helper's own journal (Comm's helper tabs) ----------------------------

def _helper_lines() -> str:
    lines = [
        {"type": "user", "isSidechain": True, "timestamp": "2026-09-25T10:00:00Z",
         "message": {"role": "user", "content": "Review the pairing door."}},
        {"type": "assistant", "isSidechain": True, "timestamp": "2026-09-25T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-5-5",
                     "content": [{"type": "text", "text": "Reading api_server.py."}]}},
    ]
    return "".join(json.dumps(line) + "\n" for line in lines)


def _helper_stub(tmp_path, provider="claude"):
    from dark_army_daemon.daemon import BobDaemon

    transcript = tmp_path / "s1.jsonl"
    transcript.write_text("")
    folder = tmp_path / "s1" / "subagents"
    folder.mkdir(parents=True)
    (folder / "agent-abc123.jsonl").write_text(_helper_lines())

    class Stub:
        def __init__(self):
            self._conversations = ConversationReader()
            self._session_states = {"s1": {"transcript_path": str(transcript)}}
            self._codex_records: dict = {}

        def _inbox_session_entry(self, sid):
            if sid != "s1":
                return None, False
            return {"session_id": "s1", "provider": provider}, False

    stub = Stub()
    stub.conversation_source = BobDaemon.conversation_source.__get__(stub)
    stub.conversation_page = BobDaemon.conversation_page.__get__(stub)
    return stub


def test_a_helpers_journal_keeps_its_brief_and_a_sessions_still_drops_sidechains(tmp_path):
    path = tmp_path / "agent.jsonl"
    path.write_text(_helper_lines())
    helper = ConversationReader().page("claude", str(path), 0, "", sidechain=True)
    assert [t["kind"] for t in helper["turns"]] == ["user", "agent"]
    assert helper["turns"][0]["text"] == "Review the pairing door."
    assert helper["provider"] == "claude"
    plain = ConversationReader().page("claude", str(path), 0, "")
    assert [t["kind"] for t in plain["turns"]] == ["agent"]


@pytest.mark.asyncio
async def test_the_daemon_pages_a_helper_from_its_sessions_own_folder(tmp_path):
    stub = _helper_stub(tmp_path)
    page = await stub.conversation_page("s1", 0, "", agent="abc123")
    assert page["available"] is True
    assert page["turns"][0]["text"] == "Review the pairing door."
    assert str(tmp_path) not in json.dumps(page)
    missing = await stub.conversation_page("s1", 0, "", agent="nope")
    assert missing == {"available": False, "provider": "claude",
                       "reason": conversation.NO_HELPER_REASON}
    for bad in ("../s1", "a/b", "x" * 65, "a.b"):
        refused = await stub.conversation_page("s1", 0, "", agent=bad)
        assert refused["available"] is False, bad
    unknown = await stub.conversation_page("other", 0, "", agent="abc123")
    assert unknown["reason"] == UNKNOWN_SESSION_REFUSAL


@pytest.mark.asyncio
async def test_a_helper_is_read_only_under_a_claude_session(tmp_path):
    stub = _helper_stub(tmp_path, provider="codex")
    page = await stub.conversation_page("s1", 0, "", agent="abc123")
    assert page["available"] is False
    assert page["reason"] == conversation.HELPER_PROVIDER_REASON


def test_the_door_passes_agent_only_when_asked_and_refuses_a_bad_one():
    class Daemon(_Daemon):
        async def conversation_page(self, session, since, key, agent=""):
            self.calls.append((session, since, key, agent))
            return dict(self._page)

    daemon = Daemon()
    srv = ApiServer(daemon, port=0)
    status, _ = _body(_run(srv._conversation_report_for("session=s1&agent=abc123")))
    assert status == 200 and daemon.calls[-1] == ("s1", 0, "", "abc123")
    # A session read still calls with three arguments: an older fake (and
    # the one above, `_Daemon`) takes no `agent`.
    old = _Daemon()
    status, _ = _body(_run(ApiServer(old, port=0)._conversation_report_for("session=s1")))
    assert status == 200 and old.calls[-1] == ("s1", 0, "")
    for bad in ("../x", "a.b", "x" * 65, "a%2Fb"):
        status, out = _body(_run(srv._conversation_report_for(
            "session=s1&agent=" + bad)))
        assert status == 400 and "error" in out, bad


def test_the_helper_marker_rides_pipeline_writable():
    from dark_army_daemon.daemon_board import BoardVerbsMixin

    class Stub(BoardVerbsMixin):
        def _observers_implementing(self, name):
            return False

        def _board_projects(self):
            return []

    assert Stub()._pipeline_writable()["subagent_conversation_supported"] is True
    models = MODELS.read_text()
    assert ("subagentConversationSupported = "
            "c.value(.subagentConversationSupported, false)") in models


def test_comm_helper_rules_run_under_swiftc(tmp_path):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    src = CONV_MODELS.read_text()
    start = src.index("enum ConversationSubject {")
    harness = r'''
var out: [String: Any] = [:]
out["key"] = ConversationSubject.key(session: "s1", agent: "abc")
out["plainKey"] = ConversationSubject.key(session: "s1")
let split = ConversationSubject.split("s1#abc")
out["split"] = [split.session, split.agent]
out["q"] = ConversationSubject.query(key: "s1#abc", since: 3, cursorKey: "k")
out["qPlain"] = ConversationSubject.query(key: "s1", since: 0, cursorKey: "")
let none = CommHelperTabs.tabs(live: [], selected: "", selectedLabel: "", supported: true)
out["none"] = none.count
let off = CommHelperTabs.tabs(live: [(id: "a", label: "Explore")], selected: "",
                              selectedLabel: "", supported: false)
out["off"] = off.count
let two = CommHelperTabs.tabs(live: [(id: "a", label: "Explore"), (id: "b", label: "")],
                              selected: "", selectedLabel: "", supported: true)
out["two"] = two.map { $0.label }
let kept = CommHelperTabs.tabs(live: [(id: "a", label: "Explore")], selected: "gone",
                               selectedLabel: "bc-verifier", supported: true)
out["kept"] = kept.map { "\($0.label):\($0.live)" }
out["sel"] = CommHelperTabs.selection("zzz", tabs: two)
let data = try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
'''
    path = tmp_path / "CommHelperProbe.swift"
    path.write_text("import Foundation\n" + src[start:] + "\n" + harness)
    exe = tmp_path / "CommHelperProbe"
    built = subprocess.run([swiftc, str(path), "-o", str(exe)],
                           capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    ran = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert ran.returncode == 0, ran.stderr
    out = json.loads(ran.stdout)
    assert out["key"] == "s1#abc" and out["plainKey"] == "s1"
    assert out["split"] == ["s1", "abc"]
    assert out["q"] == "session=s1&since=3&key=k&agent=abc"
    assert out["qPlain"] == "session=s1&since=0&key="
    assert out["none"] == 0 and out["off"] == 0
    assert out["two"] == ["Mission Control", "Explore", "b"]
    assert out["kept"] == ["Mission Control:true", "Explore:true", "bc-verifier:false"]
    assert out["sel"] == ""


def test_comm_draws_helper_tabs_read_only():
    comm = (PHONE / "CommView.swift").read_text()
    pane = comm[comm.index("struct HelperConversationPane"):]
    assert "AnswerBox(" not in pane and "terminal_input" not in pane
    assert "ConversationSubject.key(session: session, agent: agentId)" in pane
    assert 'client.watchConversation(nil, ifWatching: key)' in pane
    assert "subagentConversationSupported" in comm
    client = CLIENT.read_text()
    fetch = client[client.index("func fetchConversation("):
                   client.index("func catchUpConversation(")]
    assert "ConversationSubject.query(key: sid" in fetch


def test_a_card_questions_direction_is_not_the_persons_turn():
    """26 Sep 2026: a card's question pushed straight to the session carries
    Dark Army's direction after it; the conversation shows only the question."""
    from dark_army_daemon import daemon_board
    for name in ("bob", "dark-army", None):
        tail = daemon_board.ask_direct_tail(name)
        lead = ('<channel source="dark-army" kind="user">\nWhy two phases?\n\n'
                + tail + '\n</channel>')
        assert conversation.channel_reply_text(lead) == "Why two phases?", name
    # The sentence inside the person's own words is theirs, and stays.
    mid = ('<channel source="bob" kind="user">\n"Write one answer; change '
           'nothing else." is what it said\n</channel>')
    assert "Write one answer" in conversation.channel_reply_text(mid)
