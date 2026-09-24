"""The daemon-side reading of a hook payload (`hook_payload_to_daemon_message`).

Most cases are tables: one payload in, the fields that must come out. The
installed hook script's copy of the same reading is held to this one by
`test_notify_script.py`.
"""

import json

import pytest

from dark_army_daemon import session_stats
from dark_army_daemon.protocol import (
    hook_payload_to_daemon_message,
)

convert = hook_payload_to_daemon_message


def _fields(msg, *keys):
    return tuple(msg[key] for key in keys)


def _payload(event, **fields):
    return {"hook_event_name": event, "session_id": "sess-7", **fields}


# --- one hook, one daemon event ---

@pytest.mark.parametrize("event, fields, expected", [
    ("SessionStart", {}, {"event": "session_start"}),
    ("PreToolUse", {"tool_name": "Bash"}, {"event": "tool_use", "tool_name": "Bash"}),
    ("PreToolUse", {}, {"event": "tool_use", "tool_name": ""}),
    ("PostToolUse", {"tool_name": "AskUserQuestion"},
     {"event": "tool_done", "tool_name": "AskUserQuestion"}),
    ("PostToolUse", {}, {"event": "tool_done", "tool_name": ""}),
    ("PermissionRequest", {"tool_name": "Bash", "cwd": "/w/app"},
     {"event": "permission", "tool_name": "Bash", "project": "app"}),
    ("PostToolUseFailure", {"tool_name": "Read"}, {"event": "tool_failed", "tool_name": "Read"}),
    ("PreCompact", {}, {"event": "compact"}),
    ("Stop", {"cwd": "/w/app"},
     {"event": "add", "hook": "Stop", "message": "Waiting for input", "project": "app"}),
    ("Notification", {"notification_type": "idle_prompt", "message": "Claude is idle"},
     {"event": "add", "hook": "Notification", "message": "Claude is idle"}),
    ("Notification", {"notification_type": "idle_prompt"},
     {"event": "add", "hook": "Notification", "message": "Waiting for input"}),
    ("Notification", {"notification_type": "permission_prompt", "tool_name": "Edit"},
     {"event": "permission", "tool_name": "Edit"}),
    ("UserPromptSubmit", {}, {"event": "dismiss", "hook": "UserPromptSubmit"}),
    ("SessionEnd", {}, {"event": "dismiss", "hook": "SessionEnd"}),
    ("SubagentStart", {"agent_id": "kid-9", "agent_type": "Explore"},
     {"event": "subagent_start", "agent_id": "kid-9"}),
    ("SubagentStop", {"agent_id": "kid-9", "agent_type": "Explore"},
     {"event": "subagent_stop", "agent_id": "kid-9"}),
    ("StopFailure", {"cwd": "/w/app", "error": "The request was throttled"},
     {"event": "add", "hook": "StopFailure", "project": "app",
      "message": "The request was throttled"}),
    ("StopFailure", {}, {"event": "add", "hook": "StopFailure", "message": "API error"}),
    ("StopFailure", {"stop_reason": "max_turns"}, {"message": "max_turns"}),
])
def test_each_hook_becomes_its_daemon_event(event, fields, expected):
    msg = convert(_payload(event, **fields))
    assert msg["session_id"] == "sess-7"
    assert {key: msg.get(key) for key in expected} == expected


@pytest.mark.parametrize("payload", [
    _payload("Notification", notification_type="auth_success"),
    _payload("Notification"),
    _payload("TheNextHookNobodyHasWritten"),
    {},
])
def test_hooks_the_daemon_has_no_use_for_are_none(payload):
    assert convert(payload) is None


@pytest.mark.parametrize("payload", [
    {"hook_event_name": "UserPromptSubmit"},
    _payload("Notification", notification_type="idle_prompt", session_id=""),
])
def test_an_absent_or_empty_session_id_travels_as_empty(payload):
    assert convert(payload)["session_id"] == ""


# --- which project a card names ---

@pytest.mark.parametrize("cwd, project", [
    ("/Users/me/code/shop-front", "shop-front"),
    ("/Users/me/code/shop-front/", "shop-front"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_a_card_names_the_folder_or_unknown(cwd, project):
    fields = {"notification_type": "idle_prompt"}
    if cwd is not None:
        fields["cwd"] = cwd
    assert convert(_payload("Notification", **fields))["project"] == project


def test_an_event_that_is_not_a_card_names_no_project_without_a_folder():
    assert "project" not in convert(_payload("SessionStart"))


# --- the pid, the source, the reason ---

@pytest.mark.parametrize("event", [
    "SessionStart", "SessionEnd", "PreToolUse", "Stop", "SubagentStart",
    "PostToolUse", "PermissionRequest",
])
def test_the_payload_pid_rides_every_shape(event):
    assert convert(_payload(event, pid=4242, agent_id="kid"))["pid"] == 4242


def test_a_payload_without_pid_or_source_carries_neither():
    msg = convert(_payload("SessionStart", cwd="/w/app"))
    assert "pid" not in msg and "source" not in msg


@pytest.mark.parametrize("event, key, value", [
    ("SessionStart", "source", "clear"),
    ("SessionEnd", "reason", "logout"),
])
def test_why_a_session_began_or_ended_is_carried(event, key, value):
    assert convert(_payload(event, **{key: value}))[key] == value


# --- The question a PreToolUse is about to block on ---

def _ask_hook(tool_input, tool_name="AskUserQuestion", tool_use_id="toolu_01"):
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "abc-123",
        "cwd": "/Users/me/Projects/my-project",
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "tool_input": tool_input,
    }


def test_ask_user_question_carries_its_question():
    """The transcript learns what was asked only once somebody has answered it,
    so this event is the only timely source — see
    session_stats.question_from_tool_input."""
    msg = convert(_ask_hook({"questions": [{
        "question": "Postgres or SQLite?",
        "header": "Database",
        "options": [{"label": "Postgres", "description": "…"},
                    {"label": "SQLite", "description": "…"}],
    }]}))
    assert msg["event"] == "tool_use"
    assert msg["question"] == {
        "text": "Postgres or SQLite?",
        "options": ["Postgres", "SQLite"],
        "details": ["…", "…"],
        "header": "Database",
        "id": "toolu_01",
        # Absent `multiSelect` on the tool input is a pick-one question.
        "multi_select": False,
        "has_preview": False,
    }


def test_an_ordinary_tool_carries_none():
    msg = convert({
        "hook_event_name": "PreToolUse", "session_id": "s", "cwd": "/x/p",
        "tool_name": "Bash", "tool_input": {"command": "ls"},
    })
    assert "question" not in msg


def test_a_malformed_question_is_left_out_rather_than_raised():
    """The shape is the harness's, not ours. A future one that changes it must
    make the row quiet, not make every hook event fail."""
    for payload in ({"questions": "nope"}, {"questions": []}, {}, None,
                    {"questions": [1]}, {"questions": [{"question": "  "}]}):
        assert "question" not in convert(_ask_hook(payload))


def test_the_question_is_trimmed_before_it_crosses_the_wire():
    """An option carries a whole description and may carry a multi-line
    preview. The description is the interview's reason and rides clipped;
    the preview is a different layout and does not travel.

    Pinned against the caps themselves rather than against copies of their
    numbers: the caps are a judgement about what is worth carrying and they
    move (`MAX_QUESTION_CHARS` went 160 -> 600 when the phone started drawing
    the whole ask), and a literal here turns every such move into a failure
    that says nothing about what broke.
    """
    msg = convert(_ask_hook({"questions": [{
        "question": "q" * (session_stats.MAX_QUESTION_CHARS + 200),
        "options": [{"label": "o" * 300, "description": "d" * 900,
                     "preview": "p" * 900}] * 6,
    }]}))
    assert len(msg["question"]["text"]) == session_stats.MAX_QUESTION_CHARS
    assert len(msg["question"]["options"]) == session_stats.MAX_OPTIONS
    assert len(msg["question"]["options"][0]) == session_stats.MAX_OPTION_CHARS
    assert len(msg["question"]["details"][0]) == session_stats.MAX_DETAIL_CHARS
    # And something was actually cut, so the assertions above are a bound and
    # not an accident of a short input.
    assert msg["question"]["text"].endswith("\u2026")
    assert msg["question"]["details"][0].endswith("\u2026")
    dumped = json.dumps(msg)
    assert "d" * (session_stats.MAX_DETAIL_CHARS + 1) not in dumped
    assert "p" * 20 not in dumped


def test_a_hook_with_no_tool_use_id_still_carries_the_question():
    """The id lets a late verdict miss; without one `answer_question` tolerates
    an empty on either side rather than locking the buttons out."""
    msg = convert(_ask_hook(
        {"questions": [{"question": "Which?", "options": [{"label": "A"}]}]},
        tool_use_id=None))
    assert msg["question"]["id"] == ""
    assert msg["question"]["options"] == ["A"]


def test_grok_names_the_same_tool_in_camel_case():
    msg = convert({
        "hookEventName": "pre_tool_use", "sessionId": "s", "cwd": "/x/p",
        "toolName": "ask_user_question",
        "toolInput": {"questions": [{"question": "Which?",
                                     "options": [{"label": "A"}]}]},
    })
    assert msg["question"]["text"] == "Which?"



# --- StopFailure: the error token and the sentence ---

def test_stop_failure_translates_the_error_token():
    """The hook's `error` is a machine token, not a sentence. A hit usage limit
    arrives as `rate_limit`, and that used to be the card's whole message."""
    hook = {
        "hook_event_name": "StopFailure",
        "session_id": "s1",
        "cwd": "/tmp/proj",
        "error": "rate_limit",
    }
    msg = convert(hook)
    assert _fields(msg, "message", "error_kind") == (
        "Rate limited \u2014 wait and retry",
        "rate_limit",
    )
    assert "rate_limit" not in msg["message"]


def test_stop_failure_prefers_the_sentence_the_terminal_showed():
    hook = {
        "hook_event_name": "StopFailure",
        "session_id": "s1",
        "cwd": "/tmp/proj",
        "error": "rate_limit",
        "error_details": "429 rate_limit_error: raw body",
        "last_assistant_message": "You've hit your limit \u00b7 resets 1:40pm\n\nmore",
    }
    msg = convert(hook)
    assert _fields(msg, "message", "error_kind") == (
        "You've hit your limit \u00b7 resets 1:40pm",
        "rate_limit",
    )


def test_stop_failure_message_is_one_clamped_line():
    hook = {
        "hook_event_name": "StopFailure",
        "session_id": "s1",
        "cwd": "/tmp/proj",
        "error": "invalid_request",
        "error_details": "\n\n" + "x" * 600,
    }
    msg = convert(hook)
    assert len(msg["message"]) == 240
    assert msg["message"].endswith("\u2026")


def test_stop_failure_without_error_carries_no_kind():
    msg = convert({
        "hook_event_name": "StopFailure", "session_id": "s1", "cwd": "/tmp/proj"})
    assert "error_kind" not in msg


# --- Grok envelope (camelCase keys, snake_case event values) ---


def test_grok_session_start():
    msg = convert({
        "hookEventName": "session_start",
        "sessionId": "01a00249-19da-7a43-9152-63724f0ecbb2",
        "cwd": "/Users/me/dark-army",
    })
    assert _fields(msg, "event", "session_id", "project", "provider") == (
        "session_start",
        "01a00249-19da-7a43-9152-63724f0ecbb2",
        "dark-army",
        "claude"  # no GROK_SESSION_ID in this process,
    )


def test_grok_pre_tool_use_reads_tool_name():
    msg = convert({
        "hookEventName": "pre_tool_use",
        "sessionId": "s",
        "cwd": "/x/proj",
        "toolName": "run_terminal_command",
        "provider": "grok",
    })
    assert _fields(msg, "event", "tool_name", "provider") == (
        "tool_use",
        "run_terminal_command",
        "grok",
    )



@pytest.mark.parametrize("event, reason, shape", [
    ("stop", "end_turn", ("add", "Stop")),
    ("stop", "channel_closed", ("dismiss", "SessionEnd")),
    ("stop", "shutdown", ("dismiss", "SessionEnd")),
    ("stop_cancelled", "user_interrupt", ("add", "Stop")),
    # Only a plain Stop is read as a teardown.
    ("stop_cancelled", "shutdown", ("add", "Stop")),
])
def test_a_grok_stop_is_a_card_unless_the_tab_is_closing(event, reason, shape):
    msg = convert({"hookEventName": event, "sessionId": "g", "cwd": "/w/app",
                   "reason": reason, "provider": "grok"})
    assert (msg["event"], msg["hook"]) == shape
    if shape[0] == "dismiss":
        assert msg["reason"] == reason


def test_a_grok_session_end_with_no_role_ends_the_row_itself():
    msg = convert({"hookEventName": "session_end", "sessionId": "top",
                   "reason": "logout", "provider": "grok"})
    assert (msg["event"], msg["hook"], msg["session_id"]) == ("dismiss", "SessionEnd", "top")


def test_grok_permission_prompt_is_permission():
    msg = convert({
        "hookEventName": "notification",
        "notificationType": "permission_prompt",
        "sessionId": "s",
        "cwd": "/x/proj",
        "toolName": "run_terminal_command",
        "provider": "grok",
    })
    assert _fields(msg, "event", "tool_name") == (
        "permission",
        "run_terminal_command",
    )


def test_grok_subagent_uses_type_as_id():
    msg = convert({
        "hookEventName": "subagent_start",
        "sessionId": "s",
        "subagentType": "explore",
        "provider": "grok",
    })
    assert _fields(msg, "event", "agent_id", "session_id") == (
        "subagent_start",
        "explore",
        "s",
    )
    assert "subagent_type" not in msg


def test_grok_subagent_prefers_unique_id_over_type():
    msg = convert({
        "hookEventName": "subagent_start",
        "sessionId": "parent",
        "subagentId": "01a00c78-2cc4-7683-8471-3c2e6225034f",
        "subagentType": "bc-planner",
        "provider": "grok",
    })
    assert _fields(msg, "agent_id", "subagent_type") == (
        "01a00c78-2cc4-7683-8471-3c2e6225034f",
        "bc-planner",
    )


def test_grok_subagent_stop_lands_on_the_parent():
    msg = convert({
        "hookEventName": "subagent_stop",
        "sessionId": "child",
        "parentSessionId": "parent",
        "subagentType": "bc-planner",
        "provider": "grok",
    })
    assert _fields(msg, "event", "session_id", "parent_session_id", "agent_id") == (
        "subagent_stop",
        "parent",
        "parent",
        "bc-planner",
    )


def test_grok_child_session_end_is_a_parent_stop():
    msg = convert({
        "hookEventName": "session_end",
        "sessionId": "child",
        "parentSessionId": "parent",
        "subagentType": "bc-implementer",
        "reason": "completed",
        "provider": "grok",
    })
    assert _fields(msg, "event", "session_id", "agent_id") == (
        "subagent_stop",
        "parent",
        "bc-implementer",
    )
    assert msg.get("hook") != "SessionEnd"



def test_provider_on_hook_is_passed_through():
    msg = convert({
        "hookEventName": "user_prompt_submit",
        "sessionId": "01a0-test",
        "cwd": "/x/proj",
        "provider": "grok",
    })
    assert msg["provider"] == "grok"


def test_the_converter_carries_the_working_folder():
    """The full folder rides every shape, beside the basename `project`."""
    start = convert({
        "hook_event_name": "SessionStart",
        "session_id": "s",
        "cwd": "/x/proj/host",
    })
    assert start["cwd"] == "/x/proj/host"
    assert start["project"] == "host"

    tool = convert({
        "hook_event_name": "PreToolUse",
        "session_id": "s",
        "cwd": "/x/proj/host",
        "tool_name": "Bash",
    })
    assert tool["cwd"] == "/x/proj/host"


def test_the_converter_omits_an_absent_folder():
    msg = convert({
        "hook_event_name": "SessionStart",
        "session_id": "s",
    })
    assert "cwd" not in msg
