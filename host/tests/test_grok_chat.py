"""Grok chat_history.jsonl: last spoken assistant turn as last_text."""
import json
import os
from urllib.parse import quote

from dark_army_daemon import grok_roster
from dark_army_daemon.grok_chat import ChatCache, parse_chat
from dark_army_daemon.session_stats import (
    MAX_LAST_REPORT_CHARS,
    MAX_LAST_TEXT_CHARS,
    _collapse_prose,
)


def _assistant(content):
    return {"type": "assistant", "content": content}


def _line(obj):
    return json.dumps(obj) + "\n"


def _write(path, records):
    path.write_text("".join(_line(r) for r in records))


def _session_dir(tmp_path, cwd="/Users/me/proj", sid="sid-1"):
    directory = tmp_path / quote(cwd, safe="") / sid
    directory.mkdir(parents=True)
    return directory, cwd, sid


def test_spoken_assistant_becomes_last_text(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    raw = "Plan ready.\n\n\n- item one\n- item two  extra"
    _write(path, [_assistant(raw)])
    chat = parse_chat(str(path))
    assert chat.last_text == _collapse_prose(raw)
    assert "\n" in chat.last_text
    assert "item two extra" in chat.last_text


def test_last_text_is_tail_cut(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    raw = "x" * (MAX_LAST_TEXT_CHARS + 80)
    _write(path, [_assistant(raw)])
    chat = parse_chat(str(path))
    # One unbroken run: there is no boundary to honour, so the cut is where it
    # always was — but it says it happened, and still fits the cap.
    assert chat.last_text.startswith("… ")
    assert chat.last_text.endswith("x")
    assert len(chat.last_text) == MAX_LAST_TEXT_CHARS


def test_tool_only_turn_does_not_wipe(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    spoken = (
        "Ship it.\n"
        "<!-- bob-tldr: Ready to ship -->\n"
        "<!-- bob-actions: Accept | Iterate -->"
    )
    _write(path, [_assistant(spoken), _assistant("")])
    chat = parse_chat(str(path))
    assert chat.last_text == "Ship it."
    assert chat.last_summary == "Ready to ship"
    assert chat.last_actions == ["Accept", "Iterate"]


def test_non_assistant_types_are_ignored(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    _write(path, [
        {"type": "system", "content": "ignore me"},
        {"type": "user", "content": "please ignore"},
        {"type": "reasoning", "content": "thinking out loud"},
        _assistant("keep this"),
        {"type": "tool_result", "content": "wrote a file"},
        {"type": "backend_tool_call", "content": "search"},
        {"type": "user", "content": "still ignore"},
    ])
    chat = parse_chat(str(path))
    assert chat.last_text == "keep this"


def test_markers_are_stripped_and_a_later_spoken_turn_clears_them(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    marked = (
        "Here is the plan.\n"
        "<!-- bob-tldr: Ready to ship -->\n"
        "<!-- bob-actions: Accept | Iterate -->"
    )
    _write(path, [_assistant(marked), _assistant("Just a status report.")])
    chat = parse_chat(str(path))
    assert chat.last_text == "Just a status report."
    assert "bob-" not in chat.last_text
    assert chat.last_summary == ""
    assert chat.last_actions == []


def test_malformed_json_is_skipped(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    path.write_text(
        "not json at all\n"
        + _line(_assistant("first"))
        + "{broken\n"
        + _line(_assistant("later wins"))
    )
    chat = parse_chat(str(path))
    assert chat.last_text == "later wins"


def test_missing_file_is_empty():
    chat = parse_chat("/no/such/chat_history.jsonl")
    assert chat.last_text == ""
    assert chat.last_summary == ""
    assert chat.last_actions == []
    assert chat.question == {}
    assert chat.questions == []


def test_incremental_read_only_parses_the_delta(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    _write(path, [_assistant("one"), _assistant("two")])
    os.utime(path, (1, 1))
    cache = ChatCache()
    first = cache.get(str(path))
    assert first.last_text == "two"
    assert cache.last_folded == 2

    assert cache.get(str(path)).last_text == "two"
    assert cache.last_folded == 0

    with path.open("a", encoding="utf-8") as fh:
        fh.write(_line(_assistant("three")))
    os.utime(path, (2, 2))
    second = cache.get(str(path))
    assert cache.last_folded == 1
    assert second.last_text == "three"


def test_append_without_mtime_change_is_still_read(tmp_path):
    """Grok appends to an open chat_history.jsonl; mtime often does not move."""
    path = tmp_path / "chat_history.jsonl"
    _write(path, [_assistant("one")])
    os.utime(path, (1, 1))
    cache = ChatCache()
    assert cache.get(str(path)).last_text == "one"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_line(_ask("Use Postgres or SQLite?", ["Postgres", "SQLite"],
                            spoken="")))
    os.utime(path, (1, 1))
    got = cache.get(str(path))
    assert got.question["text"] == "Use Postgres or SQLite?"
    assert got.question["options"] == ["Postgres", "SQLite"]
    assert cache.last_folded == 1


def test_rewritten_shorter_file_rebuilds(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    long_records = [_assistant(f"line {i} " + ("pad " * 20)) for i in range(12)]
    long_records.append(_assistant("old last words"))
    _write(path, long_records)
    os.utime(path, (1, 1))
    cache = ChatCache()
    assert cache.get(str(path)).last_text == "old last words"
    long_size = path.stat().st_size

    _write(path, [_assistant("new last words")])
    assert path.stat().st_size < long_size
    os.utime(path, (2, 2))
    got = cache.get(str(path))
    assert got.last_text == "new last words"
    assert cache.last_folded == 1


def test_truncated_last_line_is_held(tmp_path):
    first = _line(_assistant("hello"))
    second = _line(_assistant("world"))
    path = tmp_path / "chat_history.jsonl"
    path.write_bytes((first + second[:20]).encode())
    os.utime(path, (1, 1))
    cache = ChatCache()
    assert cache.get(str(path)).last_text == "hello"

    path.write_bytes((first + second).encode())
    os.utime(path, (2, 2))
    got = cache.get(str(path))
    assert got.last_text == "world"
    assert cache.last_folded == 1


def test_list_content_is_not_last_text(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    _write(path, [
        _assistant("keep the spoken turn"),
        {"type": "assistant", "content": [{"type": "text", "text": "nope"}]},
    ])
    chat = parse_chat(str(path))
    assert chat.last_text == "keep the spoken turn"


def test_enrich_lands_spoken_text_on_stats(tmp_path):
    directory, cwd, sid = _session_dir(tmp_path)
    (directory / "chat_history.jsonl").write_text(
        _line(_assistant("Status: cards, cost, context, restart.")))
    stats, _metrics = grok_roster.enrich(sid, cwd, tmp_path)
    assert stats.last_text == "Status: cards, cost, context, restart."


def _ask(question, labels, *, spoken="Which do you want?"):
    arguments = json.dumps({
        "questions": [{
            "question": question,
            "options": [{"label": label} for label in labels],
        }],
    })
    return {
        "type": "assistant",
        "content": spoken,
        "tool_calls": [{
            "id": "call-ask-1",
            "name": "ask_user_question",
            "arguments": arguments,
        }],
    }


def test_numbered_list_becomes_last_actions(tmp_path):
    """The Grok fallback when ask_user_question is refused: a short numbered
    list in the spoken turn, drawn as reply_options."""
    path = tmp_path / "chat_history.jsonl"
    raw = (
        "The tree is dirty. Proceed and plan around that work?\n"
        "\n"
        "1. **Proceed** — write the plan; treat dirty files as out of scope.\n"
        "2. **Stop** — wait until the working tree is clean.\n"
    )
    _write(path, [_assistant(raw)])
    chat = parse_chat(str(path))
    assert chat.last_actions == ["Proceed", "Stop"]
    assert chat.question == {}
    assert "Proceed" in chat.last_text


REPORT = (
    "Root cause found.\n\n"
    "## Work done\n"
    "**Asked:** why Grok vanished after finishing.\n"
    "**Changed:** wrap-up refuses Grok and close-out leaves it open.\n"
    "**Verified:** the suite passed.\n"
    "**Unchecked:** Nothing - every check above ran.\n"
)


def test_last_report_survives_a_later_spoken_turn(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    _write(path, [
        _assistant(REPORT),
        _assistant("That was the leftover watcher - nothing new."),
    ])
    chat = parse_chat(str(path))
    assert chat.last_text.startswith("That was the leftover watcher")
    assert chat.last_report.startswith("## Work done")
    assert "**Unchecked:**" in chat.last_report
    assert "Root cause found" not in chat.last_report


def test_a_user_prompt_clears_last_report_and_a_tool_result_does_not(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    _write(path, [
        _assistant(REPORT),
        {"type": "user", "content": "now do the next thing"},
    ])
    chat = parse_chat(str(path))
    assert chat.last_report == ""

    _write(path, [
        _assistant(REPORT),
        {"type": "tool_result", "tool_call_id": "t1", "content": "ok"},
    ])
    mid = parse_chat(str(path))
    assert mid.last_report.startswith("## Work done")


def test_a_list_content_user_query_clears_last_report_and_synthetics_do_not(
        tmp_path):
    """Live Grok prompts are a text list with <user_query>, not a string."""
    path = tmp_path / "chat_history.jsonl"
    _write(path, [
        _assistant(REPORT),
        {"type": "user", "content": [
            {"type": "text",
             "text": "<user_query>\nnow do the next thing\n</user_query>"},
        ]},
    ])
    chat = parse_chat(str(path))
    assert chat.last_report == ""

    _write(path, [
        _assistant(REPORT),
        {"type": "user", "message": {"content": [
            {"type": "text",
             "text": "<user_query>\nfollow up\n</user_query>"},
        ]}},
    ])
    nested = parse_chat(str(path))
    assert nested.last_report == ""

    _write(path, [
        _assistant(REPORT),
        {"type": "user", "synthetic_reason": "system_reminder",
         "content": "<system-reminder>skills…"},
    ])
    synthetic = parse_chat(str(path))
    assert synthetic.last_report.startswith("## Work done")

    _write(path, [
        _assistant(REPORT),
        {"type": "user", "content": [
            {"type": "text",
             "text": "<user_info>\nOS Version: macos\n</user_info>"},
        ]},
    ])
    info = parse_chat(str(path))
    assert info.last_report.startswith("## Work done")


def test_the_latest_work_report_wins_and_a_long_one_is_marked_as_cut(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    second = REPORT.replace("why Grok vanished", "why the phone was quiet")
    _write(path, [_assistant(REPORT), _assistant(second)])
    chat = parse_chat(str(path))
    assert "why the phone was quiet" in chat.last_report
    assert "why Grok vanished" not in chat.last_report

    long = "## Work done\n" + "**Changed:** a line of the report.\n" * 400
    _write(path, [_assistant(long)])
    cut = parse_chat(str(path))
    assert len(cut.last_report) <= MAX_LAST_REPORT_CHARS
    assert cut.last_report.startswith("…")


def test_a_session_that_finished_nothing_publishes_no_work_report(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    _write(path, [_assistant("still working on it")])
    chat = parse_chat(str(path))
    assert chat.last_report == ""


def test_a_work_report_numbered_list_is_not_actions(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    raw = (
        "## Work done\n"
        "**Asked:** ship the holdings card.\n"
        "**Unchecked:**\n"
        "1. Open the app's settings and press Restart.\n"
        "2. Expect the window to come back.\n"
    )
    _write(path, [_assistant(raw)])
    chat = parse_chat(str(path))
    assert chat.last_actions == []


def test_a_list_without_a_question_is_not_actions(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    _write(path, [_assistant("Next steps:\n1. Foo\n2. Bar\n")])
    chat = parse_chat(str(path))
    assert chat.last_actions == []


def test_an_overlong_numbered_label_refuses_the_list(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    raw = (
        "Which path?\n"
        "1. A very long option label that cannot fit a button\n"
        "2. Stop\n"
    )
    _write(path, [_assistant(raw)])
    chat = parse_chat(str(path))
    assert chat.last_actions == []


def test_bob_actions_wins_over_a_numbered_list(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    raw = (
        "Ready to ship?\n"
        "1. **Accept** — do it.\n"
        "2. **Iterate** — change it.\n"
        "<!-- bob-actions: Accept | Iterate -->"
    )
    _write(path, [_assistant(raw)])
    chat = parse_chat(str(path))
    assert chat.last_actions == ["Accept", "Iterate"]


def test_ask_user_question_tool_call_becomes_question(tmp_path):
    """Grok writes the asking turn while the dialog is up. That is the
    timely source Claude's transcript is not."""
    path = tmp_path / "chat_history.jsonl"
    _write(path, [_ask("Use Postgres or SQLite?", ["Postgres", "SQLite"])])
    chat = parse_chat(str(path))
    assert chat.question["text"] == "Use Postgres or SQLite?"
    assert chat.question["options"] == ["Postgres", "SQLite"]
    assert chat.question["id"] == "call-ask-1"
    assert chat.questions[0]["index"] == 0
    assert chat.last_actions == []
    assert chat.last_text == "Which do you want?"


def test_a_later_turn_leaves_the_question_until_the_result(tmp_path):
    """Grok keeps writing assistant records while the TUI dialog is up.

    The matching tool_result is the answer; a later spoken or tool-only
    turn is the rest of the same batched turn, not proof the person
    answered. Fit-app 21 Sep 2026: `run_terminal_command` +
    `ask_user_question` on one record, then seven minutes of unanswered
    dialog that Dark Army never drew because a later assistant cleared it.
    """
    path = tmp_path / "chat_history.jsonl"
    _write(path, [
        _ask("Use Postgres or SQLite?", ["Postgres", "SQLite"]),
        _assistant("Going with Postgres."),
    ])
    chat = parse_chat(str(path))
    assert chat.question["id"] == "call-ask-1"
    assert chat.question["options"] == ["Postgres", "SQLite"]
    assert chat.last_text == "Going with Postgres."


def test_a_later_tool_only_turn_leaves_the_question(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    _write(path, [
        _ask("Use Postgres or SQLite?", ["Postgres", "SQLite"]),
        {"type": "assistant", "content": "",
         "tool_calls": [{"id": "c2", "name": "read_file",
                         "arguments": "{}"}]},
    ])
    chat = parse_chat(str(path))
    assert chat.question["id"] == "call-ask-1"
    assert chat.last_text == "Which do you want?"


def test_a_batched_ask_beside_another_tool_is_still_the_question(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    arguments = json.dumps({
        "questions": [{
            "question": "How should this run continue?",
            "options": [{"label": "Stop"}, {"label": "Hand back"}],
        }],
    })
    _write(path, [{
        "type": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "call-shell", "name": "run_terminal_command",
             "arguments": "{}"},
            {"id": "call-ask-1", "name": "ask_user_question",
             "arguments": arguments},
        ],
    }, {
        "type": "tool_result",
        "tool_call_id": "call-shell",
        "content": "exit: 0",
    }])
    chat = parse_chat(str(path))
    assert chat.question["text"] == "How should this run continue?"
    assert chat.question["options"] == ["Stop", "Hand back"]
    assert chat.question["id"] == "call-ask-1"


def test_a_genuine_user_prompt_clears_the_question(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    _write(path, [
        _ask("Use Postgres or SQLite?", ["Postgres", "SQLite"]),
        {"type": "user", "content": [
            {"type": "text",
             "text": "<user_query>\nskip the dialog\n</user_query>"},
        ]},
    ])
    chat = parse_chat(str(path))
    assert chat.question == {}
    assert chat.questions == []


def test_a_matching_tool_result_clears_the_question(tmp_path):
    """The person answered. The matching tool_result is that moment."""
    path = tmp_path / "chat_history.jsonl"
    _write(path, [
        _ask("Use Postgres or SQLite?", ["Postgres", "SQLite"]),
        {"type": "tool_result", "tool_call_id": "call-ask-1",
         "content": "User has answered your questions: "
                    '"Use Postgres or SQLite?"="Postgres".'},
    ])
    chat = parse_chat(str(path))
    assert chat.question == {}
    assert chat.questions == []
    assert chat.last_text == "Which do you want?"


def test_an_unrelated_tool_result_leaves_the_question(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    _write(path, [
        _ask("Use Postgres or SQLite?", ["Postgres", "SQLite"]),
        {"type": "tool_result", "tool_call_id": "call-other",
         "content": "read 12 lines"},
    ])
    chat = parse_chat(str(path))
    assert chat.question["id"] == "call-ask-1"
    assert chat.question["options"] == ["Postgres", "SQLite"]


def test_ask_arguments_may_be_a_dict(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    _write(path, [{
        "type": "assistant",
        "content": "Which?",
        "tool_calls": [{
            "id": "call-ask-1",
            "name": "ask_user_question",
            "arguments": {
                "questions": [{
                    "question": "Use Postgres or SQLite?",
                    "options": [{"label": "Postgres",
                                 "description": "Keep the current store."},
                                {"label": "SQLite"}],
                }],
            },
        }],
    }])
    chat = parse_chat(str(path))
    assert chat.question["text"] == "Use Postgres or SQLite?"
    assert chat.question["options"] == ["Postgres", "SQLite"]
    assert chat.question["details"] == ["Keep the current store.", ""]


def test_enrich_lands_question_on_stats(tmp_path):
    directory, cwd, sid = _session_dir(tmp_path)
    (directory / "chat_history.jsonl").write_text(
        _line(_ask("Use Postgres or SQLite?", ["Postgres", "SQLite"])))
    stats, _metrics = grok_roster.enrich(sid, cwd, tmp_path)
    assert stats.question["options"] == ["Postgres", "SQLite"]
    assert stats.questions[0]["text"] == "Use Postgres or SQLite?"


def test_enrich_keeps_a_batched_ask_after_an_unrelated_result(tmp_path):
    directory, cwd, sid = _session_dir(tmp_path)
    arguments = json.dumps({
        "questions": [{
            "question": "How should this run continue?",
            "options": [{"label": "Stop"}, {"label": "Continue"}],
        }],
    })
    (directory / "chat_history.jsonl").write_text("".join((
        _line({
            "type": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call-shell", "name": "run_terminal_command",
                 "arguments": "{}"},
                {"id": "call-ask-1", "name": "ask_user_question",
                 "arguments": arguments},
            ],
        }),
        _line({"type": "tool_result", "tool_call_id": "call-shell",
               "content": "exit: 0"}),
        _line({"type": "assistant", "content": "",
               "tool_calls": [{"id": "c2", "name": "read_file",
                               "arguments": "{}"}]}),
    )))
    stats, _metrics = grok_roster.enrich(sid, cwd, tmp_path)
    assert stats.question["text"] == "How should this run continue?"
    assert stats.question["options"] == ["Stop", "Continue"]


def test_collapse_prose_keeps_a_fenced_banner_aligned():
    """The crew's ASCII banners are boxes: every run of spaces inside a
    fence is kept, so the right edge lines up on the panel and the phone.
    Prose outside the fence is still squeezed, and trailing spaces go."""
    banner = (
        "Starting   the   check.\n"
        "```\n"
        "| > PERFECT OR NOTHING._       |   \n"
        "|                              |\n"
        "```\n"
        "Done    now."
    )
    out = _collapse_prose(banner)
    lines = out.splitlines()
    assert lines[0] == "Starting the check."
    assert lines[2] == "| > PERFECT OR NOTHING._       |"
    assert lines[3] == "|                              |"
    assert len(lines[2]) == len(lines[3])
    assert lines[-1] == "Done now."


def test_collapse_prose_unclosed_fence_runs_to_the_end():
    out = _collapse_prose("a  b\n~~~\n|  x  |")
    assert out.splitlines() == ["a b", "~~~", "|  x  |"]
