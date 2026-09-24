"""What a hook payload means to the daemon, and what a statusline payload says.

`hook_payload_to_daemon_message` is the daemon-side reading of a Claude Code
or Grok hook: one payload in, one daemon message (or None) out. The hook
script installed into every session (`NOTIFY_SCRIPT` in
`dark_army_menubar/hooks.py`) holds its own copy of the same reading,
since it imports nothing of ours; `test_notify_script.py` feeds both the same
payloads and requires the same messages.
"""

import json
from pathlib import Path
from typing import Optional

from .session_stats import clip, questions_from_tool_input

#: The notify script's broker caps (`hooks.NOTIFY_SCRIPT`), mirrored.
MAX_BROKER_DESCRIPTION_CHARS = 300
MAX_BROKER_INPUT_CHARS = 400

# The tool that puts a dialog in front of the person: its PreToolUse is the
# moment a session starts waiting on a human. The daemon's state mapping and
# the installer's PostToolUse matcher both read these names from here, the
# one module both packages already import. Grok spells it in snake_case.
ASK_USER_QUESTION_TOOL = "AskUserQuestion"
GROK_ASK_USER_QUESTION_TOOL = "ask_user_question"
ASK_USER_QUESTION_TOOLS = frozenset({
    ASK_USER_QUESTION_TOOL, GROK_ASK_USER_QUESTION_TOOL,
})
# Grok matchers are regexes and do not alias AskUserQuestion → ask_user_question,
# so the PostToolUse group has to name both or Grok never clears the waiting card.
POST_TOOL_USE_MATCHER = f"{ASK_USER_QUESTION_TOOL}|{GROK_ASK_USER_QUESTION_TOOL}"

# Grok fires Stop at session teardown as well as at turn end. A teardown Stop is
# not "waiting for input". SessionEnd was supposed to follow and often does not,
# so we map the teardown reason to SessionEnd ourselves — ending the row
# without raising a waiting card. Claude omits `reason`, so an absent value
# keeps today's turn-end behaviour.
_STOP_TEARDOWN_REASONS = frozenset({"channel_closed", "shutdown"})

# Grok's stdin envelope uses camelCase keys and snake_case event values.
# Claude uses snake_case keys and PascalCase event values. Accept both.
_EVENT_ALIASES = {
    "session_start": "SessionStart",
    "pre_tool_use": "PreToolUse",
    "post_tool_use": "PostToolUse",
    "permission_request": "PermissionRequest",
    "post_tool_use_failure": "PostToolUseFailure",
    "pre_compact": "PreCompact",
    "stop": "Stop",
    "stop_failure": "StopFailure",
    "stop_cancelled": "StopCancelled",
    "notification": "Notification",
    "user_prompt_submit": "UserPromptSubmit",
    "session_end": "SessionEnd",
    "subagent_start": "SubagentStart",
    "subagent_stop": "SubagentStop",
    "subagent_end": "SubagentStop",
}


# What a `StopFailure`'s `error` token means, in the words Claude Code itself
# uses for it (its own status table, 2.1.261). The hook's `error` is a machine
# token — `rate_limit`, `billing_error`, `overloaded` — never a sentence, and it
# used to land on the card verbatim, so a session that had hit its usage limit
# read `rate_limit` in the panel. The sentence the terminal showed rides in
# `last_assistant_message` and is preferred when present; this table is the
# fallback for a token that arrives alone. An unknown token is kept as-is.
STOP_FAILURE_WORDS = {
    "rate_limit": "Rate limited \u2014 wait and retry",
    "billing_error": "Usage limit reached \u2014 check plan",
    "overloaded": "API overloaded \u2014 wait and retry",
    "server_error": "API unavailable \u2014 retry",
    "authentication_failed": "Login required \u2014 run /login",
    "oauth_org_not_allowed": "Org disabled OAuth \u2014 use API key or ask admin",
    "account_on_hold": "Account on hold \u2014 see detail",
    "invalid_request": "Invalid API request \u2014 see detail",
    "model_not_found": "Model not found",
    "max_output_tokens": "Reply hit the output limit",
    "unknown": "API error",
}

# A card message is one line the pane draws in orange; an API error body can be
# a paragraph. First non-empty line, clamped.
STOP_FAILURE_MESSAGE_CHARS = 240


def _stop_failure_line(value):
    if not isinstance(value, str):
        return ""
    for line in value.splitlines():
        line = line.strip()
        if line:
            if len(line) > STOP_FAILURE_MESSAGE_CHARS:
                line = line[:STOP_FAILURE_MESSAGE_CHARS - 1].rstrip() + "\u2026"
            return line
    return ""


def stop_failure_message(hook, get):
    """The sentence a `StopFailure` card shows, and the error token beside it.

    `last_assistant_message` is what the terminal printed — for a hit usage
    limit, "You've hit your limit \u00b7 resets 1:40pm" — so it wins. Then the
    raw API message, then the token translated, then the older `stop_reason`
    shape, then the stock words."""
    token = get(hook, "error")
    token = token if isinstance(token, str) else ""
    for key in ("last_assistant_message", "lastAssistantMessage",
                "error_details", "errorDetails"):
        text = _stop_failure_line(get(hook, key))
        if text:
            return text, token
    if token.strip():
        return STOP_FAILURE_WORDS.get(token, token), token
    reason = get(hook, "stop_reason", "stopReason")
    return (reason if isinstance(reason, str) and reason else "API error"), token


def is_ask_user_question(tool_name: str) -> bool:
    return tool_name in ASK_USER_QUESTION_TOOLS


def _hook_get(hook: dict, *keys, default=""):
    """First present value among Claude snake_case and Grok camelCase keys."""
    for key in keys:
        if key in hook and hook[key] is not None:
            return hook[key]
    return default


def _canon_event(raw) -> str:
    if not isinstance(raw, str) or not raw:
        return ""
    return _EVENT_ALIASES.get(raw, raw)


def _project_of(cwd) -> str:
    if not cwd:
        return ""
    return Path(str(cwd)).name


def _subagent_agent_id(hook: dict) -> str:
    """Prefer a unique child id. Grok's type is a last resort.

    ``subagentType`` is the role (``bc-planner``), reused every spawn.
    Using it as the set key made sequential /ship stages accumulate as
    +4 live children. Unique keys (``subagent_id`` / ``agent_id``) are
    one instance.
    """
    unique = _hook_get(hook, "subagent_id", "subagentId", "agent_id", "agentId")
    typed = _hook_get(hook, "subagentType", "subagent_type")
    return str(unique or typed or "")


def _subagent_type(hook: dict) -> str:
    return str(_hook_get(hook, "subagentType", "subagent_type") or "")


def _parent_session_id(hook: dict) -> str:
    return str(_hook_get(hook, "parent_session_id", "parentSessionId") or "")


def _subagent_daemon_message(
    hook: dict, event: str, *, session_id, project, pid, provider, cwd="",
) -> dict:
    """Start/stop attributed to the *parent* session.

    Grok's SubagentStop (and a child's SessionEnd) fire inside the child,
    whose ``sessionId`` is not the row we drew. ``parentSessionId`` is the
    parent; without it the daemon still sees the child's id and the
    discard misses.
    """
    agent_id = _subagent_agent_id(hook)
    typed = _subagent_type(hook)
    parent = _parent_session_id(hook)
    msg = _with_common(
        {"event": event, "agent_id": agent_id},
        session_id=parent or session_id, project=project, pid=pid,
        provider=provider, cwd=cwd,
    )
    if typed and typed != agent_id:
        msg["subagent_type"] = typed
    if parent:
        msg["parent_session_id"] = parent
    return msg


def _provider_of(hook: dict) -> str:
    marked = hook.get("provider")
    if marked in ("grok", "claude"):
        return marked
    return "claude"


def _with_common(msg: dict, *, session_id, project, pid, provider, cwd="") -> dict:
    msg["session_id"] = session_id
    msg["provider"] = provider
    # The full folder, beside the basename: the daemon stores it so a session
    # in a subdirectory can resolve to its VS Code workspace.
    if cwd:
        msg["cwd"] = str(cwd)
    if project:
        msg["project"] = project
    if pid is not None:
        msg["pid"] = pid
    return msg


# Hook events that become one daemon event naming the tool, and nothing else.
_TOOL_EVENTS = {
    "PostToolUse": "tool_done",
    "PermissionRequest": "permission",
    "PostToolUseFailure": "tool_failed",
}


def _stamped(seen: dict, body: dict, project=None) -> dict:
    return _with_common(
        body, session_id=seen["session_id"],
        project=seen["project"] if project is None else project,
        pid=seen["pid"], provider=seen["provider"], cwd=seen["cwd"],
    )


def _card(seen: dict, hook_name: str, text: str, **extra) -> dict:
    """An `add`: the card the panel raises for a session that has stopped.
    A card always names a project, if only as "unknown"."""
    body = {"event": "add", "hook": hook_name, "message": text,
            "transcript_path": seen["transcript_path"], **extra}
    return _stamped(seen, body, project=seen["project"] or "unknown")


def _ended(seen: dict, reason) -> dict:
    msg = _stamped(seen, {"event": "dismiss", "hook": "SessionEnd"})
    if reason is not None:
        msg["reason"] = reason
    return msg


def _subagent(seen: dict, event: str) -> dict:
    return _subagent_daemon_message(
        seen["hook"], event, session_id=seen["session_id"], project=seen["project"],
        pid=seen["pid"], provider=seen["provider"], cwd=seen["cwd"],
    )


def _on_session_start(hook: dict, seen: dict) -> dict:
    msg = _stamped(seen, {"event": "session_start"})
    source = _hook_get(hook, "source", default=None)
    if source is not None:
        msg["source"] = source
    return msg


def _on_tool_start(hook: dict, seen: dict) -> dict:
    tool = seen["tool_name"]
    msg = _stamped(seen, {"event": "tool_use", "tool_name": tool})
    if not is_ask_user_question(tool):
        return msg
    # A dialog's words exist nowhere else in time: the transcript records the
    # asking turn only once it has been answered, so they ride this message.
    asked = questions_from_tool_input(
        _hook_get(hook, "tool_input", "toolInput", default=None),
        str(_hook_get(hook, "tool_use_id", "toolUseId") or ""),
    )
    if asked:
        # `question` (the first, without its position) is the key every
        # installed client decodes and stays a single object for ever; the
        # whole dialog is its sibling, which older readers never look at.
        first = {k: v for k, v in asked[0].items() if k != "index"}
        msg["question"] = first
        msg["questions"] = asked
    return msg


def _on_compact(hook: dict, seen: dict) -> dict:
    return _stamped(seen, {"event": "compact"})


def _on_stop(hook: dict, seen: dict) -> dict:
    reason = _hook_get(hook, "reason", default=None)
    if seen["event"] == "Stop" and reason in _STOP_TEARDOWN_REASONS:
        # Grok's tab-close Stop: the session is over, and the SessionEnd that
        # should follow often never does. Ending the row here raises no
        # "waiting" card for a session nobody can answer.
        return _ended(seen, reason)
    return _card(seen, "Stop", "Waiting for input")


def _on_stop_failure(hook: dict, seen: dict) -> dict:
    text, token = stop_failure_message(hook, _hook_get)
    return _card(seen, "StopFailure", text, **({"error_kind": token} if token else {}))


def _on_notification(hook: dict, seen: dict) -> Optional[dict]:
    kind = _hook_get(hook, "notification_type", "notificationType")
    if kind == "permission_prompt":
        # The notify script's twin: the prompt's own words and its input, each
        # clipped to the broker's caps, so a Notification-only ask still
        # names what it wants.
        tool_input = _hook_get(hook, "tool_input", "toolInput") or {}
        extra = {"event": "permission", "tool_name": seen["tool_name"]}
        message = _hook_get(hook, "message")
        if isinstance(message, str):
            extra["description"] = clip(message.strip(), MAX_BROKER_DESCRIPTION_CHARS)
        if isinstance(tool_input, dict) and tool_input:
            try:
                preview = json.dumps(tool_input, separators=(",", ":"),
                                     ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                preview = ""
            extra["input_preview"] = clip(preview, MAX_BROKER_INPUT_CHARS)
        return _stamped(seen, extra)
    if kind == "idle_prompt":
        return _card(seen, "Notification", _hook_get(hook, "message") or "Waiting for input")
    return None


def _on_prompt(hook: dict, seen: dict) -> dict:
    return _stamped(seen, {"event": "dismiss", "hook": "UserPromptSubmit"})


def _on_session_end(hook: dict, seen: dict) -> dict:
    # Grok ends a child session inside the child, typed with its role. That
    # is one of the parent's subagents leaving, not the parent ending.
    if _subagent_type(hook):
        return _subagent(seen, "subagent_stop")
    return _ended(seen, _hook_get(hook, "reason", default=None))


_BUILDERS = {
    "SessionStart": _on_session_start,
    "PreToolUse": _on_tool_start,
    "PreCompact": _on_compact,
    "Stop": _on_stop,
    "StopCancelled": _on_stop,
    "StopFailure": _on_stop_failure,
    "Notification": _on_notification,
    "UserPromptSubmit": _on_prompt,
    "SessionEnd": _on_session_end,
    "SubagentStart": lambda hook, seen: _subagent(seen, "subagent_start"),
    "SubagentStop": lambda hook, seen: _subagent(seen, "subagent_stop"),
}


def hook_payload_to_daemon_message(hook: dict) -> Optional[dict]:
    """The daemon message a Claude Code or Grok hook payload stands for, or
    None for an event the daemon has no use for.

    Keys are read in either spelling (Claude's snake_case, Grok's camelCase)
    and event names in either case style; the pid is the one the payload
    carries, when it carries one."""
    event = _canon_event(_hook_get(hook, "hook_event_name", "hookEventName"))
    cwd = _hook_get(hook, "cwd")
    seen = {
        "hook": hook,
        "event": event,
        "session_id": _hook_get(hook, "session_id", "sessionId"),
        "cwd": cwd,
        "project": _project_of(cwd),
        "pid": hook.get("pid"),
        "provider": _provider_of(hook),
        "tool_name": _hook_get(hook, "tool_name", "toolName"),
        "transcript_path": _hook_get(hook, "transcript_path", "transcriptPath"),
    }
    if event in _TOOL_EVENTS:
        return _stamped(seen, {"event": _TOOL_EVENTS[event], "tool_name": seen["tool_name"]})
    build = _BUILDERS.get(event)
    return build(hook, seen) if build else None


def _num(value):
    """Numbers only. Claude Code omits fields rather than nulling them, but a
    string sneaking into a token count would poison every downstream sum."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def flatten_statusline(payload: dict) -> dict:
    """Reduce a raw statusline payload to the flat metrics the daemon stores.

    Unknown/absent fields come back as None rather than 0 — "we have not been
    told" and "it is zero" are different, and a cost of 0.0 shown for a session
    that simply has no data yet is a lie the UI would repeat."""
    if not isinstance(payload, dict):
        return {}

    def sub(key):
        value = payload.get(key)
        return value if isinstance(value, dict) else {}

    model, cost, ctx = sub("model"), sub("cost"), sub("context_window")
    limits, workspace, repo = sub("rate_limits"), sub("workspace"), sub("workspace").get("repo")
    repo = repo if isinstance(repo, dict) else {}
    five, seven = (limits.get("five_hour") or {}), (limits.get("seven_day") or {})
    pr = sub("pr")

    return {
        "session_id": payload.get("session_id") or "",
        "session_name": payload.get("session_name") or "",
        "model_id": model.get("id") or "",
        "model_name": model.get("display_name") or "",
        "effort": (sub("effort").get("level") or ""),
        "fast_mode": bool(payload.get("fast_mode")),
        "thinking": bool(sub("thinking").get("enabled")),
        "cost_usd": _num(cost.get("total_cost_usd")),
        "duration_ms": _num(cost.get("total_duration_ms")),
        "api_duration_ms": _num(cost.get("total_api_duration_ms")),
        "lines_added": _num(cost.get("total_lines_added")),
        "lines_removed": _num(cost.get("total_lines_removed")),
        "ctx_used_pct": _num(ctx.get("used_percentage")),
        "ctx_size": _num(ctx.get("context_window_size")),
        "ctx_input_tokens": _num(ctx.get("total_input_tokens")),
        "ctx_output_tokens": _num(ctx.get("total_output_tokens")),
        "exceeds_200k": bool(payload.get("exceeds_200k_tokens")),
        "five_hour_pct": _num(five.get("used_percentage")),
        "five_hour_resets_at": _num(five.get("resets_at")),
        "seven_day_pct": _num(seven.get("used_percentage")),
        "seven_day_resets_at": _num(seven.get("resets_at")),
        "cwd": payload.get("cwd") or workspace.get("current_dir") or "",
        "project_dir": workspace.get("project_dir") or "",
        "git_worktree": workspace.get("git_worktree") or "",
        "repo": "/".join(p for p in (repo.get("owner"), repo.get("name")) if p),
        "pr_number": _num(pr.get("number")),
        "pr_url": pr.get("url") or "",
        "pr_review_state": pr.get("review_state") or "",
        "agent_name": sub("agent").get("name") or "",
        "cc_version": payload.get("version") or "",
        "transcript_path": payload.get("transcript_path") or "",
    }
