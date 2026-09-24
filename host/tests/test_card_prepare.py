"""Dark Army writing a new card's instructions from a plain-language description.

The helper subprocess is never run. Each isolation flag is asserted on its
own so a failure names the guarantee that was dropped.
"""
import asyncio
import json
import logging
from pathlib import Path

import pytest

from dark_army_daemon import (
    agents_poll, board, card_prepare, card_preparer_brief, dispatch, paths,
)
from dark_army_daemon.daemon import BobDaemon


def _argv(**fields):
    kwargs = {"title": "t", "summary": "s", "tool": "claude", "project": "p"}
    kwargs.update(fields)
    return card_prepare.argv("claude", **kwargs)


#: A closed set for the parse tests: the filter drops anything off-roster,
#: so every test that expects stages back names its roster.
_ROSTER = [("bc-planner", "writes the plan"),
           ("bc-implementer", "builds what the plan says")]


def _agent_file(directory, name, description="does a thing"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\nmodel: haiku\n---\n"
        "Body text the reader never needs.\n",
        encoding="utf-8")
    return path


# --- the command line ------------------------------------------------------


def test_the_helper_writes_no_transcript():
    assert "--no-session-persistence" in _argv()


def test_the_helper_loads_no_settings_and_therefore_no_hooks():
    args = _argv()
    i = args.index("--setting-sources")
    assert args[i + 1] == ""


def test_the_helper_starts_no_mcp_servers():
    assert "--strict-mcp-config" in _argv()


def test_the_helper_runs_the_cheap_model_and_the_given_binary():
    args = card_prepare.argv("/opt/claude", title="t", summary="s",
                             tool="claude", project="p")
    assert args[0] == "/opt/claude"
    assert args[args.index("--model") + 1] == card_prepare.MODEL


# --- The helper is the card's own assistant, on that assistant's cheap model


def test_every_dispatchable_tool_has_a_cheap_helper_model():
    assert set(card_prepare.HELPER_MODELS) == set(dispatch._EXECUTABLES)
    assert card_prepare.HELPER_MODELS["claude"] == card_prepare.MODEL
    for tool, model in card_prepare.HELPER_MODELS.items():
        assert model and not model.startswith("-"), tool
        # Every helper model is one the curated list offers: an off-list name
        # is one the CLI may have stopped serving, as Codex's did in Sep 2026.
        assert model in dispatch.MODELS[tool], tool


@pytest.mark.parametrize("tool, expected", [
    ("claude", "claude"), ("codex", "codex"), ("grok", "grok"),
    ("Codex", "codex"), ("", "claude"), (None, "claude"),
    ("cursor", "claude"), ("--codex", "claude"),
])
def test_helper_tool_is_the_assistant_where_known_else_claude(tool, expected):
    assert card_prepare.helper_tool(tool) == expected


_GROK_AGENT = "/tmp/card-preparer.grok.md"


def test_a_codex_card_is_prepared_by_codex_exec_on_its_small_model():
    args = card_prepare.argv("/opt/codex", title="t", summary="s",
                             tool="codex", project="p")
    assert args[:2] == ["/opt/codex", "exec"]
    assert args[args.index("--model") + 1] == card_prepare.HELPER_MODELS["codex"]
    assert "--ephemeral" in args            # no journal for codex_rollouts
    assert "--ignore-user-config" in args   # no MCP servers, no board copy
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert args[-2] == "--"                 # the prompt can never be a flag
    data = card_prepare.prompt_for(
        title="t", summary="s", tool="codex", project="p")
    assert args[-1].startswith(card_preparer_brief.BRIEF)
    assert args[-1].endswith(data)
    assert args[-1] == card_preparer_brief.BRIEF + "\n\n" + data
    assert "ASSISTANT: codex" in args[-1]
    for flag in ("-p", "--allowed-tools", "--setting-sources",
                 "--no-session-persistence", "--strict-mcp-config", "--agent"):
        assert flag not in args, flag


def test_a_grok_card_is_prepared_by_grok_single_turn_on_its_small_model():
    args = card_prepare.argv("/opt/grok", title="t", summary="s",
                             tool="grok", project="p", brief_path=_GROK_AGENT)
    assert args[0] == "/opt/grok"
    assert "exec" not in args
    assert args[args.index("--model") + 1] == card_prepare.HELPER_MODELS["grok"]
    assert args[args.index("--output-format") + 1] == "plain"
    for flag in ("--no-subagents", "--disable-web-search", "--no-plan"):
        assert flag in args, flag
    assert args[-4:-2] == ["--agent", _GROK_AGENT]
    assert args[-2] == "-p"
    assert args[-1] == card_prepare.prompt_for(
        title="t", summary="s", tool="grok", project="p")
    for flag in ("--allowed-tools", "--setting-sources",
                 "--no-session-persistence", "--strict-mcp-config"):
        assert flag not in args, flag


@pytest.mark.parametrize("tool, exe, chosen", [
    ("claude", "/opt/claude", "sonnet"),
    ("codex", "/opt/codex", "gpt-5.5"),
    ("grok", "/opt/grok", "grok-4.6"),
])
def test_argv_model_swaps_the_flag_and_nothing_else(tool, exe, chosen):
    """`argv(model=)`: the card-preparer slot of Dark Army's Agent models
    setting lands on `--model`; every other element is untouched."""
    kw = dict(title="t", summary="s", tool=tool, project="p",
              brief_path=_GROK_AGENT)
    plain = card_prepare.argv(exe, **kw)
    picked = card_prepare.argv(exe, model=chosen, **kw)
    assert picked[picked.index("--model") + 1] == chosen
    assert plain[plain.index("--model") + 1] == card_prepare.HELPER_MODELS[tool]
    assert [a for a in plain if a != card_prepare.HELPER_MODELS[tool]] == [
        a for a in picked if a != chosen]


@pytest.mark.parametrize("tool, exe", [
    ("claude", "/opt/claude"), ("codex", "/opt/codex"), ("grok", "/opt/grok"),
])
def test_argv_with_an_empty_model_is_byte_identical(tool, exe):
    kw = dict(title="t", summary="s", tool=tool, project="p",
              brief_path=_GROK_AGENT)
    assert card_prepare.argv(exe, model="", **kw) == card_prepare.argv(exe, **kw)
    assert card_prepare.argv(exe, model=None, **kw) == card_prepare.argv(exe, **kw)


def test_argv_with_the_shipped_luna_model_is_todays_codex_line():
    kw = dict(title="t", summary="s", tool="codex", project="p")
    assert card_prepare.argv("/opt/codex", model="gpt-6-luna", **kw) == (
        card_prepare.argv("/opt/codex", **kw))


def test_attachments_widen_only_the_claude_helper():
    paths = ("/tmp/a.png", "/tmp/b.png")
    for tool in ("codex", "grok"):
        extra = {"brief_path": _GROK_AGENT} if tool == "grok" else {}
        args = card_prepare.argv("/opt/x", title="t", summary="s", tool=tool,
                                 project="p", attachments=paths, **extra)
        assert "--allowed-tools" not in args
        assert args[-1] == card_prepare.argv(
            "/opt/x", title="t", summary="s", tool=tool, project="p",
            attachments=paths, **extra)[-1]
        assert "ATTACHMENTS:\n/tmp/a.png\n/tmp/b.png" in args[-1]


def test_an_unknown_assistant_falls_back_to_the_claude_line_byte_for_byte():
    known = card_prepare.argv("/opt/claude", title="t", summary="s",
                              tool="claude", project="p")
    unknown = card_prepare.argv("/opt/claude", title="t", summary="s",
                                tool="cursor", project="p")
    assert unknown[0] == known[0]
    assert unknown[1] == known[1] == "-p"
    assert unknown[3:] == known[3:]          # every flag identical
    assert "ASSISTANT: cursor" in unknown[2]  # the prompt still names it


def test_the_claude_helper_gets_a_cleaned_environment():
    """Never inherit the frozen app's PYTHONHOME — a child interpreter would
    boot against the bundle."""
    base = {"PATH": "/usr/bin", "PYTHONHOME": "/Apps/Dark Army.app/Contents/Resources",
            "HOME": "/Users/x", "BOB_COMPANION_PORT": "19873"}
    env = card_prepare.env_for("claude", base=base)
    assert env is not None
    assert "PYTHONHOME" not in env
    assert env["PATH"] == "/usr/bin"
    assert env.get("BOB_COMPANION_PORT") == "19873"  # claude does not rewrite it
    assert "DARK_ARMY_HOOK_SOCKET" not in env          # nor adds the quiet socket
    assert card_prepare.env_for("") is not None
    assert card_prepare.env_for("cursor") is not None


@pytest.mark.parametrize("tool", ["codex", "grok"])
def test_the_other_helpers_get_a_hook_port_nobody_answers(tool):
    base = {"PATH": "/usr/bin", "BOB_COMPANION_PORT": "19873",
            "CLAWD_TANK_PORT": "19873", "HOME": "/Users/x"}
    env = card_prepare.env_for(tool, base=base)
    assert env is not base
    assert env["BOB_COMPANION_PORT"] == card_prepare.QUIET_HOOK_PORT
    assert "CLAWD_TANK_PORT" not in env      # the legacy name must not win
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/Users/x"
    assert base["BOB_COMPANION_PORT"] == "19873"  # never mutated
    # A port nothing listens on: refused, so the notify script drops the event.
    assert 0 < int(card_prepare.QUIET_HOOK_PORT) < 1024
    # And the private socket's silence, for a notify script from after it:
    # under the one address rule an explicit socket is the only address.
    assert env["DARK_ARMY_HOOK_SOCKET"] == card_prepare.QUIET_HOOK_SOCKET
    assert "DARK_ARMY_HOOK_SOCKET" not in base


def test_the_quiet_socket_refuses_a_connect_at_once():
    """`/dev/null` exists on every Mac, is not a socket, and fails a connect
    immediately — nothing is read and nothing waits."""
    import socket
    import time
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    started = time.monotonic()
    try:
        with pytest.raises(OSError):
            probe.connect(card_prepare.QUIET_HOOK_SOCKET)
    finally:
        probe.close()
    assert time.monotonic() - started < 0.1


def test_the_instruction_travels_in_the_prompt():
    args = _argv(summary="fix the sprite pipeline")
    assert "--system-prompt" not in args
    prompt = args[args.index("-p") + 1]
    assert prompt.startswith(card_prepare.MODE_HEAD)
    assert "imperative" not in prompt
    assert "fix the sprite pipeline" in prompt


def test_mcp_config_is_absent():
    assert "--mcp-config" not in _argv()


def test_argv_without_attachments_has_no_allowed_tools_and_ends_as_before():
    args = _argv()
    assert "--allowed-tools" not in args
    assert "Read" not in args
    i = args.index("--setting-sources")
    assert args[i + 1] == ""
    assert args[-2:] == ["--agent", card_preparer_brief.NAME]
    again = _argv()
    assert args == again


def test_argv_with_two_attachments_grants_one_scoped_read_each():
    paths = ("/tmp/a.png", "/tmp/b.pdf")
    args = _argv(attachments=paths)
    assert "--allowed-tools" in args
    i = args.index("--allowed-tools")
    rules = args[i + 1:]
    assert rules == [f"Read(//{p})" for p in paths]
    assert "Read" not in rules


def test_prompt_for_without_attachments_keeps_the_head_and_has_no_section():
    prompt = card_prepare.prompt_for(
        title="t", summary="s", tool="claude", project="p")
    assert prompt.startswith(card_prepare.MODE_HEAD)
    assert "imperative" not in prompt
    assert "Do not use any tools." not in prompt
    assert "ATTACHMENTS:" not in prompt


def test_prompt_for_with_attachments_lists_them_and_leaves_the_brief_to_argv():
    paths = ("/tmp/a.png", "/tmp/b.pdf")
    prompt = card_prepare.prompt_for(
        title="t", summary="s", tool="claude", project="p",
        attachments=paths)
    assert "Do not use any tools." not in prompt
    assert "Use the Read tool to open each file listed under ATTACHMENTS" not in prompt
    assert "ATTACHMENTS:\n/tmp/a.png\n/tmp/b.pdf\n" in prompt
    assert prompt.index("PROJECT:") < prompt.index("ATTACHMENTS:")


def test_attachment_timeout_is_strictly_longer():
    assert (card_prepare.TIMEOUT_WITH_ATTACHMENTS_SECONDS
            > card_prepare.TIMEOUT_SECONDS)


def test_the_summary_is_cut_to_the_store_ceiling():
    prompt = card_prepare.prompt_for(
        title="t", summary="x" * 5000, tool="claude", project="p")
    body = prompt[prompt.index("DESCRIPTION: ") + len("DESCRIPTION: "):]
    body = body.split("\n", 1)[0]
    assert len(body) == card_prepare.MAX_SUMMARY_INPUT
    assert card_prepare.MAX_SUMMARY_INPUT == board.MAX_SUMMARY_CHARS


@pytest.mark.asyncio
async def test_prepare_does_not_length_refuse_a_paragraph_summary():
    """600 characters used to bounce at the 400 ceiling. A paragraph is now
    legitimate; the next cheap check (no folder) is what fires."""
    daemon = BobDaemon()
    result, detail = await daemon.prepare_card_text({"summary": "x" * 600})
    assert result is None
    assert "longer than" not in (detail or "")
    assert "folder" in (detail or "")


@pytest.mark.asyncio
async def test_prepare_refuses_a_summary_over_the_store_ceiling():
    daemon = BobDaemon()
    result, detail = await daemon.prepare_card_text(
        {"summary": "x" * (board.MAX_SUMMARY_CHARS + 1)})
    assert result is None
    assert "2000" in (detail or "")


# --- reading the answer ----------------------------------------------------


def test_a_clean_two_section_answer_is_the_prompt_and_the_helpers():
    prompt, stages = card_prepare.parse(
        "INSTRUCTIONS:\nFix the panel cursor.\n\n"
        "SPECIALISTS:\nbc-planner\nbc-implementer\n", roster=_ROSTER)
    assert prompt == "Fix the panel cursor."
    assert stages == ["bc-planner", "bc-implementer"]


def test_an_answer_wrapped_in_fences_still_parses():
    prompt, stages = card_prepare.parse(
        "```\nINSTRUCTIONS:\nFix the panel cursor.\n\n"
        "SPECIALISTS:\nbc-planner\n```\n", roster=_ROSTER)
    assert prompt == "Fix the panel cursor."
    assert stages == ["bc-planner"]


def test_a_preamble_before_instructions_is_ignored():
    prompt, stages = card_prepare.parse(
        "Sure! Here you go:\n\n"
        "INSTRUCTIONS:\nFix the panel cursor.\n\n"
        "SPECIALISTS:\nbc-planner\n", roster=_ROSTER)
    assert prompt == "Fix the panel cursor."
    assert stages == ["bc-planner"]


def test_missing_specialists_is_an_empty_list_and_the_prompt_is_kept():
    prompt, stages = card_prepare.parse("INSTRUCTIONS:\nFix the panel cursor.\n")
    assert prompt == "Fix the panel cursor."
    assert stages == []


def test_missing_instructions_is_refused_not_salvaged():
    prompt, stages = card_prepare.parse(
        "Sure, I can help.\n\nSPECIALISTS:\nbc-planner\n")
    assert prompt == ""
    assert stages == []


def test_specialists_none_is_an_empty_list():
    prompt, stages = card_prepare.parse(
        "INSTRUCTIONS:\nFix the panel cursor.\n\nSPECIALISTS:\nNONE\n")
    assert prompt == "Fix the panel cursor."
    assert stages == []


def test_none_with_a_roster_in_hand_is_still_an_empty_list():
    # The NONE short-circuit runs before any roster matching: a model that
    # has a menu and judges none of it relevant still parses to [].
    prompt, stages = card_prepare.parse(
        "INSTRUCTIONS:\nFix the panel cursor.\n\nSPECIALISTS:\nNONE\n",
        roster=_ROSTER)
    assert prompt == "Fix the panel cursor."
    assert stages == []


def test_more_than_max_stages_is_clamped():
    names = [f"stage-{i}" for i in range(board.MAX_STAGES + 5)]
    roster = [(name, "a helper") for name in names]
    prompt, stages = card_prepare.parse(
        "INSTRUCTIONS:\nDo the work.\n\nSPECIALISTS:\n" + "\n".join(names),
        roster=roster)
    assert prompt == "Do the work."
    assert len(stages) == board.MAX_STAGES
    assert stages == names[:board.MAX_STAGES]


def test_a_stage_at_the_char_ceiling_survives_the_filter_whole():
    # An overlong name is now excluded at `read_roster` (see the reader
    # tests); a name at the ceiling passes the filter and `parse_stages`
    # unclipped, so the prompt's spelling and the stored spelling agree.
    name = "x" * board.MAX_STAGE_CHARS
    prompt, stages = card_prepare.parse(
        f"INSTRUCTIONS:\nDo the work.\n\nSPECIALISTS:\n{name}\n",
        roster=[(name, "a helper")])
    assert prompt == "Do the work."
    assert stages == [name]


# --- the closed-set filter -------------------------------------------------


def test_an_off_roster_name_is_dropped():
    _, stages = card_prepare.parse(
        "INSTRUCTIONS:\nDo the work.\n\n"
        "SPECIALISTS:\nbc-planner\nmade-up-helper\n", roster=_ROSTER)
    assert stages == ["bc-planner"]


def test_a_trailing_parenthetical_is_recognised_anyway():
    _, stages = card_prepare.parse(
        "INSTRUCTIONS:\nDo the work.\n\n"
        "SPECIALISTS:\nbc-planner (writes the plan)\n", roster=_ROSTER)
    assert stages == ["bc-planner"]


def test_trailing_chatter_after_the_name_is_recognised_anyway():
    _, stages = card_prepare.parse(
        "INSTRUCTIONS:\nDo the work.\n\n"
        "SPECIALISTS:\nbc-planner — writes the plan\n", roster=_ROSTER)
    assert stages == ["bc-planner"]


def test_a_match_is_canonicalised_to_the_rosters_own_spelling():
    _, stages = card_prepare.parse(
        "INSTRUCTIONS:\nDo the work.\n\nSPECIALISTS:\nBC-PLANNER\n",
        roster=_ROSTER)
    assert stages == ["bc-planner"]


def test_the_models_order_is_preserved():
    _, stages = card_prepare.parse(
        "INSTRUCTIONS:\nDo the work.\n\n"
        "SPECIALISTS:\nbc-implementer\nbc-planner\n", roster=_ROSTER)
    assert stages == ["bc-implementer", "bc-planner"]


def test_an_empty_roster_keeps_nothing():
    # The model was shown no helpers and told to answer NONE; anything else
    # is invention, and Dark Army never invents generic stand-ins.
    prompt, stages = card_prepare.parse(
        "INSTRUCTIONS:\nDo the work.\n\n"
        "SPECIALISTS:\nbc-planner\nreviewer\n")
    assert prompt == "Do the work."
    assert stages == []


def test_a_prefix_never_claims_a_longer_roster_name():
    # Whole-item first, first-token second, never substring: `bc-plan` must
    # not claim `bc-planner`.
    _, stages = card_prepare.parse(
        "INSTRUCTIONS:\nDo the work.\n\nSPECIALISTS:\nbc-plan\n",
        roster=_ROSTER)
    assert stages == []


# --- the prompt's roster block ---------------------------------------------


def test_the_roster_rides_in_the_prompt():
    args = _argv(roster=_ROSTER)
    prompt = args[args.index("-p") + 1]
    assert prompt.startswith(card_prepare.MODE_HEAD)
    assert "- bc-planner — writes the plan" in prompt
    assert "- bc-implementer — builds what the plan says" in prompt
    # Data, never str()'d: no list repr leaks into the prompt.
    assert "[(" not in prompt
    assert "')" not in prompt


def test_an_empty_roster_says_so_and_asks_for_none():
    args = _argv()
    prompt = args[args.index("-p") + 1]
    assert prompt.startswith(card_prepare.MODE_HEAD)
    assert "There are no helpers available for this project." in prompt
    assert "Write NONE under SPECIALISTS." in prompt
    assert "- " not in prompt.split("TITLE:")[0].replace(
        card_prepare.MODE_HEAD, "")


# --- frontmatter -----------------------------------------------------------


def test_a_folded_description_joins_to_one_spaced_line():
    parsed = card_prepare.parse_frontmatter(
        "---\nname: bc-verifier\n"
        "description: Checks the work\n"
        "  against the plan's own\n"
        "  acceptance criteria.\n"
        "tools: Read\n---\nBody.\n")
    assert parsed == ("bc-verifier",
                      "Checks the work against the plan's own "
                      "acceptance criteria.")


def test_text_without_fences_is_not_frontmatter():
    assert card_prepare.parse_frontmatter("name: x\ndescription: y\n") is None


def test_frontmatter_without_a_name_is_none():
    assert card_prepare.parse_frontmatter(
        "---\ndescription: something\n---\n") is None


def test_frontmatter_without_a_closing_fence_is_none():
    assert card_prepare.parse_frontmatter("---\nname: x\n") is None


# --- the reader ------------------------------------------------------------


def test_the_reader_reads_both_directories(tmp_path):
    root = tmp_path / "project"
    _agent_file(root / ".claude" / "agents", "bc-planner", "writes the plan")
    user = tmp_path / "home-agents"
    _agent_file(user, "helper-two", "a personal helper")
    roster = card_prepare.read_roster(str(root), user_dir=str(user))
    assert roster == [("bc-planner", "writes the plan"),
                      ("helper-two", "a personal helper")]


def test_a_project_entry_shadows_a_same_name_user_entry(tmp_path):
    root = tmp_path / "project"
    _agent_file(root / ".claude" / "agents", "bc-planner", "the project one")
    user = tmp_path / "home-agents"
    _agent_file(user, "BC-Planner", "the personal one")
    roster = card_prepare.read_roster(str(root), user_dir=str(user))
    assert roster == [("bc-planner", "the project one")]


def test_a_malformed_file_is_skipped_and_its_siblings_survive(tmp_path):
    root = tmp_path / "project"
    agents = root / ".claude" / "agents"
    _agent_file(agents, "bc-planner", "writes the plan")
    (agents / "broken.md").write_text("no frontmatter here\n", encoding="utf-8")
    (agents / "empty-name.md").write_text("---\nname:\n---\n", encoding="utf-8")
    roster = card_prepare.read_roster(
        str(root), user_dir=str(tmp_path / "absent"))
    assert roster == [("bc-planner", "writes the plan")]


def test_a_missing_directory_is_an_empty_roster(tmp_path):
    roster = card_prepare.read_roster(
        str(tmp_path / "no-such-project"),
        user_dir=str(tmp_path / "no-such-home"))
    assert roster == []


def test_an_overlong_frontmatter_name_never_enters_the_roster(tmp_path):
    root = tmp_path / "project"
    agents = root / ".claude" / "agents"
    _agent_file(agents, "x" * (board.MAX_STAGE_CHARS + 1), "too long to store")
    _agent_file(agents, "bc-planner", "writes the plan")
    roster = card_prepare.read_roster(
        str(root), user_dir=str(tmp_path / "absent"))
    assert roster == [("bc-planner", "writes the plan")]


def test_the_roster_stops_at_its_ceiling(tmp_path):
    root = tmp_path / "project"
    agents = root / ".claude" / "agents"
    for i in range(card_prepare.MAX_ROSTER_AGENTS + 5):
        _agent_file(agents, f"helper-{i:03d}")
    roster = card_prepare.read_roster(
        str(root), user_dir=str(tmp_path / "absent"))
    assert len(roster) == card_prepare.MAX_ROSTER_AGENTS


def test_a_description_is_collapsed_and_clamped(tmp_path):
    root = tmp_path / "project"
    _agent_file(root / ".claude" / "agents", "bc-planner",
                "word " * 200)
    roster = card_prepare.read_roster(
        str(root), user_dir=str(tmp_path / "absent"))
    (name, description), = roster
    assert name == "bc-planner"
    assert len(description) == card_prepare.MAX_ROSTER_DESC_CHARS
    assert "  " not in description


def test_non_md_files_and_subdirectories_are_ignored(tmp_path):
    root = tmp_path / "project"
    agents = root / ".claude" / "agents"
    _agent_file(agents, "bc-planner", "writes the plan")
    (agents / "notes.txt").write_text(
        "---\nname: not-an-agent\n---\n", encoding="utf-8")
    # A directory whose own name ends in .md, holding an agent file: neither
    # the directory nor its contents may enter the roster (no recursion).
    _agent_file(agents / "folder.md", "buried-agent", "should not be seen")
    roster = card_prepare.read_roster(
        str(root), user_dir=str(tmp_path / "absent"))
    assert roster == [("bc-planner", "writes the plan")]


def _preparer_file(directory, name="bc-card-preparer",
                   description=None, body=None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    desc = description if description is not None else card_preparer_brief.DESCRIPTION
    text = (
        f"---\nname: {name}\ndescription: {desc}\ntools: Read\n---\n\n"
        + (body if body is not None else card_preparer_brief.BRIEF)
    )
    path.write_text(text, encoding="utf-8")
    return path


def test_the_preparer_is_not_in_the_roster(tmp_path):
    root = tmp_path / "project"
    agents = root / ".claude" / "agents"
    for name, desc in (
        ("bc-planner", "writes the plan"),
        ("bc-implementer", "builds it"),
        ("bc-verifier", "checks it"),
        ("bc-bug-auditor", "audits it"),
        ("bc-integration-reviewer", "reviews it"),
        ("bc-card-preparer", "drafts cards"),
    ):
        _agent_file(agents, name, desc)
    roster = card_prepare.read_roster(
        str(root), user_dir=str(tmp_path / "absent"))
    names = [n for n, _ in roster]
    assert names == [
        "bc-bug-auditor", "bc-implementer", "bc-integration-reviewer",
        "bc-planner", "bc-verifier",
    ]
    assert "bc-card-preparer" not in names


def test_a_vendored_preparer_is_also_excluded_from_the_roster(tmp_path):
    root = tmp_path / "project"
    agents = root / ".claude" / "agents"
    _agent_file(agents, "bc-planner", "writes the plan")
    _agent_file(agents, "xx-card-preparer", "drafts cards")
    roster = card_prepare.read_roster(
        str(root), user_dir=str(tmp_path / "absent"))
    assert roster == [("bc-planner", "writes the plan")]


def test_clean_specialists_drops_a_model_answer_naming_the_preparer():
    stages = card_prepare._clean_specialists(
        "bc-planner\nbc-card-preparer\n", roster=_ROSTER)
    assert stages == ["bc-planner"]


def test_is_preparer_name_matches_the_role_and_a_prefixed_copy():
    assert card_prepare.is_preparer_name("card-preparer")
    assert card_prepare.is_preparer_name("bc-card-preparer")
    assert card_prepare.is_preparer_name("XX-Card-Preparer")
    assert not card_prepare.is_preparer_name("bc-planner")
    assert not card_prepare.is_preparer_name("card-preparer-extra")
    assert not card_prepare.is_preparer_name("")


def test_read_brief_prefers_the_project_copy(tmp_path):
    root = tmp_path / "project"
    user = tmp_path / "home-agents"
    project_body = card_preparer_brief.BRIEF.replace(
        "no more than 14 words", "no more than 14 words (project)")
    user_body = card_preparer_brief.BRIEF.replace(
        "no more than 14 words", "no more than 14 words (user)")
    _preparer_file(root / ".claude" / "agents", body=project_body)
    _preparer_file(user, body=user_body)
    brief = card_prepare.read_brief(str(root), user_dir=str(user))
    assert brief.source == "project"
    assert brief.name == "bc-card-preparer"
    assert "(project)" in brief.body
    assert "(user)" not in brief.body


def test_read_brief_prefers_the_user_copy_over_bundled(tmp_path):
    root = tmp_path / "project"
    user = tmp_path / "home-agents"
    user_body = card_preparer_brief.BRIEF.replace(
        "no more than 14 words", "no more than 14 words (user)")
    _preparer_file(user, body=user_body)
    brief = card_prepare.read_brief(str(root), user_dir=str(user))
    assert brief.source == "user"
    assert "(user)" in brief.body


def test_read_brief_accepts_a_prefixed_preparer_name(tmp_path):
    root = tmp_path / "project"
    body = card_preparer_brief.BRIEF
    _preparer_file(root / ".claude" / "agents", name="xx-card-preparer",
                   body=body)
    brief = card_prepare.read_brief(
        str(root), user_dir=str(tmp_path / "absent"))
    assert brief.source == "project"
    assert brief.name == "xx-card-preparer"
    assert brief.body == body


@pytest.mark.parametrize("body", [
    card_preparer_brief.BRIEF.replace(card_prepare._NO_TOOLS, ""),
    card_preparer_brief.BRIEF + card_prepare._NO_TOOLS + "\n",
    card_preparer_brief.BRIEF.replace("FOLDER:", "PLACE:"),
])
def test_a_brief_that_fails_brief_ok_falls_back_to_bundled(tmp_path, body):
    root = tmp_path / "project"
    path = _preparer_file(root / ".claude" / "agents", body=body)
    brief = card_prepare.read_brief(
        str(root), user_dir=str(tmp_path / "absent"))
    assert brief.source == "bundled"
    assert brief.body == card_preparer_brief.BRIEF
    assert brief.name == card_preparer_brief.NAME
    assert path.is_file()


def test_a_missing_directory_is_the_bundled_brief(tmp_path):
    brief = card_prepare.read_brief(
        str(tmp_path / "no-such-project"),
        user_dir=str(tmp_path / "no-such-home"))
    assert brief == card_prepare.Brief(
        card_preparer_brief.NAME,
        card_preparer_brief.DESCRIPTION,
        card_preparer_brief.BRIEF,
        "bundled",
    )


def test_read_brief_strips_frontmatter(tmp_path):
    root = tmp_path / "project"
    _preparer_file(root / ".claude" / "agents")
    brief = card_prepare.read_brief(
        str(root), user_dir=str(tmp_path / "absent"))
    assert brief.source == "project"
    assert not brief.body.lstrip().startswith("---")
    assert "name: bc-card-preparer" not in brief.body
    assert brief.body == card_preparer_brief.BRIEF


def test_a_multibyte_char_split_by_the_brief_ceiling_keeps_the_file(tmp_path):
    root = tmp_path / "project"
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    head = (
        "---\nname: bc-card-preparer\n"
        "description: drafts cards\n---\n"
    )
    required = (
        "TITLE: SUMMARY: INSTRUCTIONS: SPECIALISTS: FOLDER:\n"
        "Do not use any tools.\n"
    )
    filler = "x" * (card_prepare.BRIEF_READ_BYTES - len(head) - len(required) - 1)
    (agents / "bc-card-preparer.md").write_text(
        head + required + filler + "é tail", encoding="utf-8")
    brief = card_prepare.read_brief(
        str(root), user_dir=str(tmp_path / "absent"))
    assert brief.source == "project"
    assert card_prepare.brief_ok(brief.body)


# --- refusals --------------------------------------------------------------


def test_a_prompt_starting_with_a_dash_is_refused():
    reason = card_prepare.refusal("-please-fix-the-tests", "claude")
    assert reason
    assert "option" in reason


def test_a_claude_subcommand_word_is_refused():
    reason = card_prepare.refusal("mcp", "claude")
    assert reason
    assert "mcp" in reason


def test_a_codex_subcommand_is_refused_even_with_no_tool():
    # `apply` is only on codex. With no assistant chosen the union of every
    # `_SUBCOMMANDS` set still turns it away, so picking one later cannot
    # make a prepared card unstartable.
    assert "apply" in dispatch._SUBCOMMANDS["codex"]
    assert "apply" not in dispatch._SUBCOMMANDS["claude"]
    reason = card_prepare.refusal("apply", "")
    assert reason
    assert "apply" in reason


def test_an_overlong_prompt_is_refused_not_truncated():
    long = "Do the work. " + ("x" * (card_prepare.MAX_PROMPT_OUT + 1))
    reason = card_prepare.refusal(long, "claude")
    assert reason
    assert str(card_prepare.MAX_PROMPT_OUT) in reason
    assert "truncat" not in reason.lower()
    # And parse itself does not cut it either.
    prompt, _ = card_prepare.parse("INSTRUCTIONS:\n" + long)
    assert len(prompt) > card_prepare.MAX_PROMPT_OUT


def test_agent_trail_appears_nowhere_in_the_module():
    src = Path(card_prepare.__file__).read_text(encoding="utf-8")
    assert "agent_trail" not in src


def test_a_quoted_frontmatter_name_enters_the_roster_unquoted():
    parsed = card_prepare.parse_frontmatter(
        '---\nname: "bc-planner"\ndescription: \'writes the plan\'\n---\n')
    assert parsed == ("bc-planner", "writes the plan")


def test_a_byte_order_mark_before_the_fence_does_not_hide_the_file():
    parsed = card_prepare.parse_frontmatter(
        "﻿---\nname: bc-planner\ndescription: writes the plan\n---\n")
    assert parsed == ("bc-planner", "writes the plan")


def test_a_multibyte_char_split_by_the_read_ceiling_keeps_the_file(tmp_path):
    # The cut at ROSTER_READ_BYTES can land mid-character; the frontmatter at
    # the top of the file is intact either way and must still be read.
    root = tmp_path / "project"
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    head = "---\nname: bc-planner\ndescription: writes the plan\n---\n"
    filler = "x" * (card_prepare.ROSTER_READ_BYTES - len(head) - 1)
    (agents / "bc-planner.md").write_text(
        head + filler + "é tail", encoding="utf-8")
    roster = card_prepare.read_roster(
        str(root), user_dir=str(tmp_path / "absent"))
    assert roster == [("bc-planner", "writes the plan")]


class _FakeProc:
    def __init__(self, out: bytes, code: int = 0):
        self._out = out
        self.returncode = code

    async def communicate(self):
        return self._out, b""

    def kill(self):  # pragma: no cover
        raise AssertionError("must not be killed")


@pytest.mark.asyncio
async def test_prepare_never_bakes_attachment_paths(tmp_path, monkeypatch):
    """A stored prompt carries no attachment links at all. The block is
    dispatch's job, added once at the moment an assistant is started."""
    root = tmp_path / "project"
    root.mkdir()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"ok")
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    monkeypatch.setattr(agents_poll, "find_claude_binary",
                        lambda: "/bin/claude")
    monkeypatch.setattr(card_prepare, "read_roster", lambda _root: [])

    async def fake_exec(*args, **kwargs):
        return _FakeProc(
            b"INSTRUCTIONS:\nFix the usage wrap.\n\nSPECIALISTS:\nNONE\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "t", "summary": "s", "tool": "claude",
         "project": "p", "attachment_paths": [str(shot)]},
        str(root))
    assert result is not None, detail
    assert result["prompt"] == "Fix the usage wrap."
    assert str(shot) not in result["prompt"]
    assert "Attached files (open with your file tools):" not in result["prompt"]


@pytest.mark.asyncio
async def test_prepare_runs_on_the_card_preparer_slot(tmp_path, monkeypatch):
    """`_agent_model_for(root, helper, "card-preparer")` at the fourth site:
    the chosen preparer model reaches the helper's argv, and Default keeps
    `HELPER_MODELS`' cheap name."""
    root = tmp_path / "project"
    root.mkdir()
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    monkeypatch.setattr(agents_poll, "find_claude_binary",
                        lambda: "/bin/claude")
    monkeypatch.setattr(card_prepare, "read_roster", lambda _root: [])
    seen = []

    async def fake_exec(*args, **kwargs):
        seen.append(list(args))
        return _FakeProc(
            b"INSTRUCTIONS:\nFix the usage wrap.\n\nSPECIALISTS:\nNONE\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    fields = {"title": "t", "summary": "s", "tool": "claude", "project": "p"}
    result, detail = await daemon._prepare_card_text_locked(fields, str(root))
    assert result is not None, detail
    assert seen[-1][seen[-1].index("--model") + 1] == card_prepare.MODEL

    daemon.set_agent_model_override(str(root), {"claude": {"card-preparer": "sonnet"}})
    result, detail = await daemon._prepare_card_text_locked(fields, str(root))
    assert result is not None, detail
    assert seen[-1][seen[-1].index("--model") + 1] == "sonnet"


@pytest.mark.parametrize("tail", [
    # What `_WITH_ATTACHMENTS` actually produced: bare paths, no header.
    "{shot}\n",
    "- {shot}\n",
    "Attached files (open with your file tools):\n{shot}\n",
])
@pytest.mark.asyncio
async def test_prepare_strips_bare_paths_the_helper_already_wrote(
        tmp_path, monkeypatch, tail):
    """The doubling reproduction. A helper that lists the paths anyway cannot
    bake them in: each path appears zero times in the stored prompt."""
    root = tmp_path / "project"
    root.mkdir()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"ok")
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    monkeypatch.setattr(agents_poll, "find_claude_binary",
                        lambda: "/bin/claude")
    monkeypatch.setattr(card_prepare, "read_roster", lambda _root: [])
    answer = ("INSTRUCTIONS:\nFix the wrap.\n\n"
              + tail.format(shot=shot)
              + "\nSPECIALISTS:\nNONE\n")

    async def fake_exec(*args, **kwargs):
        return _FakeProc(answer.encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "t", "summary": "s", "tool": "claude",
         "project": "p", "attachment_paths": [str(shot)]},
        str(root))
    assert result is not None, detail
    assert result["prompt"].count(str(shot)) == 0
    assert result["prompt"].startswith("Fix the wrap.")


@pytest.mark.asyncio
async def test_prepare_refuses_an_answer_that_was_only_paths(
        tmp_path, monkeypatch):
    """Strip before refusal, or a paths-only answer stores as a blank prompt
    instead of being refused as not usable."""
    root = tmp_path / "project"
    root.mkdir()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"ok")
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    monkeypatch.setattr(agents_poll, "find_claude_binary",
                        lambda: "/bin/claude")
    monkeypatch.setattr(card_prepare, "read_roster", lambda _root: [])
    answer = f"INSTRUCTIONS:\n{shot}\n\nSPECIALISTS:\nNONE\n"

    async def fake_exec(*args, **kwargs):
        return _FakeProc(answer.encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "t", "summary": "s", "tool": "claude",
         "project": "p", "attachment_paths": [str(shot)]},
        str(root))
    assert result is None
    assert detail


# --- the one-box idea mode -------------------------------------------------


def test_an_empty_idea_leaves_the_command_line_byte_identical():
    """The legacy press must not change by one byte. Widening the prompt
    under everybody's existing habit is how a working feature regresses."""
    assert _argv(idea="") == _argv()
    assert _argv(idea="   ") == _argv()


def test_the_idea_prompt_asks_for_seven_labelled_sections_in_order():
    """The three objective labels sit between SUMMARY and INSTRUCTIONS, so
    INSTRUCTIONS is still immediately followed by SPECIALISTS and the
    trailing FOLDER: answer still lands inside the specialists block."""
    body = card_prepare.prompt_for(
        title="", summary="", tool="claude", project="p",
        idea="make the strip stop wrapping")
    assert body.startswith(card_prepare.MODE_HEAD_IDEA)
    assert "imperative" not in body
    labels = ("TITLE:", "SUMMARY:", "BENEFICIARY:", "BENEFIT:", "CRITERION:",
              "INSTRUCTIONS:", "SPECIALISTS:")
    head = card_prepare.MODE_HEAD_IDEA
    positions = [head.index(label) for label in labels]
    assert positions == sorted(positions), positions
    for label in labels:
        assert label in body
    assert "IDEA: make the strip stop wrapping" in body


def test_the_idea_mode_never_sends_the_title_or_description_fields():
    """In idea mode those two are what the helper is being asked to write;
    feeding it a half-typed title would be asking it to agree with itself."""
    body = card_prepare.prompt_for(
        title="a stale title", summary="a stale description",
        tool="claude", project="p", idea="the real thought")
    assert "a stale title" not in body
    assert "a stale description" not in body
    assert "\nTITLE: " not in body
    assert "DESCRIPTION:" not in body


def test_the_idea_is_collapsed_and_clamped():
    long_idea = "word " * 3000
    body = card_prepare.prompt_for(
        title="", summary="", tool="claude", project="p", idea=long_idea)
    line = [l for l in body.splitlines() if l.startswith("IDEA: ")][0]
    assert len(line) - len("IDEA: ") == card_prepare.MAX_IDEA_INPUT


def test_the_attachment_swap_still_bites_in_idea_mode():
    """`_brief`'s `replace(..., 1)` is silent when the sentence is missing, so
    a reworded brief would drop the Read grant's instructions."""
    assert card_prepare._NO_TOOLS in card_preparer_brief.BRIEF
    body = card_prepare.prompt_for(
        title="", summary="", tool="claude", project="p",
        idea="an idea", attachments=("/tmp/shot.png",))
    assert card_prepare._NO_TOOLS not in body
    assert "ATTACHMENTS:\n/tmp/shot.png" in body
    args = _argv(idea="an idea", attachments=("/tmp/shot.png",))
    payload = json.loads(args[args.index("--agents") + 1])
    prompt = payload[card_preparer_brief.NAME]["prompt"]
    assert card_prepare._NO_TOOLS not in prompt
    assert "Use the Read tool to open each file listed under ATTACHMENTS" in prompt


def test_parse_idea_reads_all_four_sections():
    title, summary, prompt, stages = card_prepare.parse_idea(
        "TITLE: Stop the strip wrapping\n"
        "SUMMARY: The menu bar wraps when three agents run.\n"
        "INSTRUCTIONS: Measure the strip and step the ladder down.\n"
        "SPECIALISTS:\nbc-planner\n",
        roster=_ROSTER)
    assert title == "Stop the strip wrapping"
    assert summary == "The menu bar wraps when three agents run."
    assert prompt == "Measure the strip and step the ladder down."
    assert stages == ["bc-planner"]


def test_parse_idea_survives_a_fenced_answer():
    title, summary, prompt, stages = card_prepare.parse_idea(
        "```\nTITLE: A short name\nSUMMARY: One sentence.\n"
        "INSTRUCTIONS: Do the thing.\nSPECIALISTS: NONE\n```",
        roster=_ROSTER)
    assert (title, summary, prompt, stages) == (
        "A short name", "One sentence.", "Do the thing.", [])


def test_parse_idea_without_instructions_salvages_nothing():
    assert card_prepare.parse_idea(
        "TITLE: A name\nSUMMARY: A sentence.\n") == ("", "", "", [])


def test_parse_idea_reads_a_missing_title_as_empty():
    """Empty, so `title_refusal` refuses the whole call — never a partial
    fill that reads as success."""
    title, summary, prompt, _stages = card_prepare.parse_idea(
        "SUMMARY: A sentence.\nINSTRUCTIONS: Do it.\nSPECIALISTS: NONE\n")
    assert title == ""
    assert summary == "A sentence."
    assert prompt == "Do it."
    assert card_prepare.title_refusal(title)


def test_parse_idea_drops_a_list_marker_and_quotes_off_the_title():
    title, _s, _p, _st = card_prepare.parse_idea(
        'TITLE:\n- "Stop the strip wrapping"\n'
        "SUMMARY: One sentence.\nINSTRUCTIONS: Do it.\n")
    assert title == "Stop the strip wrapping"


def test_the_legacy_parser_ignores_a_summary_label():
    """The second regex earns its keep: a chatty legacy answer that writes
    SUMMARY: inside its instructions parses exactly as it does today."""
    prompt, _stages = card_prepare.parse(
        "INSTRUCTIONS:\nWrite the SUMMARY: line last.\nSPECIALISTS: NONE\n")
    assert prompt == "Write the SUMMARY: line last."


@pytest.mark.parametrize("title", [
    "",
    "   ",
    "a title that keeps going and going " * 4,
    "one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen",
])
def test_a_title_that_is_not_a_title_is_refused_not_truncated(title):
    reason = card_prepare.title_refusal(title)
    assert reason


def test_a_real_title_is_accepted():
    assert card_prepare.title_refusal("Stop the strip wrapping") is None


def test_a_multiline_title_is_refused():
    assert card_prepare.title_refusal("A name\nand a second line")


@pytest.mark.parametrize("summary", ["", "   ", "one\ntwo",
                                     "x" * (board.MAX_SUMMARY_CHARS + 1)])
def test_a_description_that_is_not_one_is_refused(summary):
    assert card_prepare.summary_refusal(summary)


def test_a_real_description_is_accepted():
    assert card_prepare.summary_refusal(
        "The menu bar wraps when three agents run at once.") is None


@pytest.mark.asyncio
async def test_the_idea_press_fills_all_four_fields(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    monkeypatch.setattr(agents_poll, "find_claude_binary",
                        lambda: "/bin/claude")
    monkeypatch.setattr(card_prepare, "read_roster", lambda _root: _ROSTER)
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["argv"] = args
        return _FakeProc(
            b"TITLE: Stop the strip wrapping\n"
            b"SUMMARY: The menu bar wraps when three agents run.\n"
            b"INSTRUCTIONS: Measure the strip and step the ladder down.\n"
            b"SPECIALISTS:\nbc-planner\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "", "summary": "", "tool": "claude", "project": "p",
         "idea": "the strip wraps when three agents run",
         "attachment_paths": []},
        str(root))
    assert result is not None, detail
    assert result["title"] == "Stop the strip wrapping"
    assert result["summary"] == "The menu bar wraps when three agents run."
    assert result["prompt"] == "Measure the strip and step the ladder down."
    assert result["workflow"] == "bc-planner"
    assert "IDEA: the strip wraps when three agents run" in " ".join(
        str(a) for a in seen["argv"])


@pytest.mark.asyncio
async def test_a_legacy_press_answers_with_exactly_two_keys(
        tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    monkeypatch.setattr(agents_poll, "find_claude_binary",
                        lambda: "/bin/claude")
    monkeypatch.setattr(card_prepare, "read_roster", lambda _root: [])

    async def fake_exec(*args, **kwargs):
        return _FakeProc(b"INSTRUCTIONS:\nDo the thing.\nSPECIALISTS: NONE\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "t", "summary": "s", "tool": "claude", "project": "p",
         "attachment_paths": []},
        str(root))
    assert result is not None, detail
    # `suggested_root` rides every answer (empty here — no closed set was
    # offered); the point of this test is that legacy mode writes neither
    # `title` nor `summary`, which are the person's own input.
    # Delivery areas: the additive suggestion rides both Prepare modes.
    assert set(result) == {"prompt", "workflow", "suggested_root", "suggested_area"}
    assert result["suggested_root"] == ""
    # And none of the objective keys: the legacy branch's reply is unchanged.
    for key in ("beneficiary", "intended_benefit", "success_criterion"):
        assert key not in result


@pytest.mark.asyncio
async def test_a_bad_title_refuses_the_whole_idea_press(tmp_path, monkeypatch):
    """A partial fill would look like success — three boxes written and one
    left as it was, with nothing saying which."""
    root = tmp_path / "project"
    root.mkdir()
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    monkeypatch.setattr(agents_poll, "find_claude_binary",
                        lambda: "/bin/claude")
    monkeypatch.setattr(card_prepare, "read_roster", lambda _root: [])

    async def fake_exec(*args, **kwargs):
        return _FakeProc(
            b"TITLE: " + b"a very long sentence about the work " * 4
            + b"\nSUMMARY: One sentence.\nINSTRUCTIONS: Do it.\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "", "summary": "", "tool": "claude", "project": "p",
         "idea": "an idea", "attachment_paths": []},
        str(root))
    assert result is None
    assert "title" in detail


@pytest.mark.asyncio
async def test_an_idea_needs_no_typed_description(tmp_path, monkeypatch):
    """The gate that would otherwise refuse the one-box press before any
    model call: `prepare_card_text` requires a description, and an idea is
    the description's replacement rather than a second copy of it."""
    root = tmp_path / "project"
    root.mkdir()
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    daemon._known_project_roots = lambda: [str(root)]
    monkeypatch.setattr(agents_poll, "find_claude_binary",
                        lambda: "/bin/claude")
    monkeypatch.setattr(card_prepare, "read_roster", lambda _root: [])

    async def fake_exec(*args, **kwargs):
        return _FakeProc(
            b"TITLE: A short name\nSUMMARY: One sentence.\n"
            b"INSTRUCTIONS: Do it.\nSPECIALISTS: NONE\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result, detail = await daemon.prepare_card_text(
        {"summary": "", "idea": "one whole thought", "tool": "claude",
         "project": "p", "root": str(root)})
    assert result is not None, detail
    assert result["title"] == "A short name"


@pytest.mark.asyncio
async def test_an_overlong_idea_is_refused_before_any_model_call(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    daemon._known_project_roots = lambda: [str(root)]
    result, detail = await daemon.prepare_card_text(
        {"summary": "", "tool": "claude", "project": "p", "root": str(root),
         "idea": "x" * (card_prepare.MAX_IDEA_INPUT + 1)})
    assert result is None
    assert str(card_prepare.MAX_IDEA_INPUT) in detail


# --- the folder suggestion -------------------------------------------------

#: Two roots, so the block is offered at all. Distinct basenames, so the
#: basename rung is unambiguous unless a test says otherwise.
_ROOTS = ["/Users/x/Code/dark-army", "/Users/x/Code/shop"]


def test_one_project_is_no_menu_and_no_block():
    """With one choice the picker is already on it; an opinion about it is
    noise, not help."""
    assert card_prepare._folders_block(()) == ""
    assert card_prepare._folders_block(["/one"]) == ""


def test_two_projects_list_every_root_and_ask_for_one_line():
    block = card_prepare._folders_block(_ROOTS)
    assert "FOLDER:" in block
    assert "NONE" in block
    for root in _ROOTS:
        assert f"- {root}" in block


def test_the_folder_menu_stops_at_its_ceiling():
    roots = [f"/tmp/p{i}" for i in range(card_prepare.MAX_PROJECT_CHOICES + 5)]
    block = card_prepare._folders_block(roots)
    assert block.count("\n- ") == card_prepare.MAX_PROJECT_CHOICES
    assert f"- {roots[card_prepare.MAX_PROJECT_CHOICES]}" not in block


def test_no_roots_leaves_the_prompt_and_argv_byte_identical():
    """The legacy press must not change by one byte for somebody with one
    project open."""
    for idea in ("", "one whole thought"):
        assert card_prepare.prompt_for(
            title="t", summary="s", tool="claude", project="p",
            roster=_ROSTER, idea=idea, roots=()) == card_prepare.prompt_for(
                title="t", summary="s", tool="claude", project="p",
                roster=_ROSTER, idea=idea)
    assert _argv(roots=()) == _argv()
    assert _argv(roots=(), idea="x") == _argv(idea="x")


def test_the_folder_menu_rides_between_the_roster_and_the_fields():
    """Order is load-bearing: the head stays the prompt's prefix, and the
    fields stay last."""
    for idea, first_field in (("", "TITLE:"), ("a thought", "IDEA:")):
        body = card_prepare.prompt_for(
            title="t", summary="s", tool="claude", project="p",
            roster=_ROSTER, idea=idea, roots=_ROOTS)
        assert body.index("bc-planner") < body.index("FOLDER:")
        assert body.index("FOLDER:") < body.index(first_field)


def test_an_exact_answer_is_the_root():
    raw = ("INSTRUCTIONS:\nDo it.\nSPECIALISTS:\nNONE\n"
           f"FOLDER:\n{_ROOTS[1]}\n")
    assert card_prepare.parse_folder(raw, _ROOTS) == _ROOTS[1]


def test_a_case_different_answer_is_canonicalised_to_the_supplied_root():
    """A path in an answer is untrusted text: what comes back is always the
    string Dark Army supplied, never the one the helper typed."""
    raw = f"FOLDER:\n{_ROOTS[0].upper()}\n"
    assert card_prepare.parse_folder(raw, _ROOTS) == _ROOTS[0]


def test_a_unique_basename_is_enough():
    assert card_prepare.parse_folder("FOLDER:\nshop\n", _ROOTS) == _ROOTS[1]


def test_an_ambiguous_basename_is_no_suggestion():
    roots = ["/Users/x/a/shop", "/Users/x/b/shop"]
    assert card_prepare.parse_folder("FOLDER:\nshop\n", roots) == ""


def test_a_list_marker_and_quotes_come_off_the_answer():
    raw = f'FOLDER:\n- "{_ROOTS[0]}"\n'
    assert card_prepare.parse_folder(raw, _ROOTS) == _ROOTS[0]


def test_the_last_folder_label_is_the_one_read():
    raw = f"FOLDER:\n{_ROOTS[0]}\nFOLDER:\n{_ROOTS[1]}\n"
    assert card_prepare.parse_folder(raw, _ROOTS) == _ROOTS[1]


@pytest.mark.parametrize("raw", [
    "FOLDER:\nNONE\n",
    "FOLDER:\n\n",
    "INSTRUCTIONS:\nDo it.\n",
    "FOLDER:\n/somewhere/else\n",
    "FOLDER:\n~/invented/path\n",
])
def test_nothing_usable_is_no_suggestion(raw):
    assert card_prepare.parse_folder(raw, _ROOTS) == ""


def test_an_empty_closed_set_can_never_produce_a_suggestion():
    assert card_prepare.parse_folder(f"FOLDER:\n{_ROOTS[0]}\n", ()) == ""


def test_a_fenced_answer_still_yields_the_folder():
    raw = f"```\nINSTRUCTIONS:\nDo it.\nFOLDER:\n{_ROOTS[0]}\n```"
    assert card_prepare.parse_folder(raw, _ROOTS) == _ROOTS[0]


def test_the_section_regexes_were_not_widened():
    """Adding FOLDER to either regex makes a legacy answer whose instructions
    mention "FOLDER:" parse as a truncated prompt."""
    assert card_prepare._SECTION.pattern == r"(INSTRUCTIONS|SPECIALISTS):"
    # `_SECTION_IDEA` tolerates markdown decoration on its labels (the
    # tolerant-parse plan) but names no FOLDER and no AREA: both keep
    # landing inside SPECIALISTS, where `_clean_specialists` drops them.
    assert "FOLDER" not in card_prepare._SECTION_IDEA.pattern
    assert "AREA" not in card_prepare._SECTION_IDEA.pattern
    assert card_prepare._IDEA_LABELS == (
        "TITLE|SUMMARY|BENEFICIARY|BENEFIT|CRITERION|INSTRUCTIONS|SPECIALISTS")


def test_a_trailing_folder_section_leaves_the_instructions_whole():
    raw = ("INSTRUCTIONS:\nFix the strip wrap.\nSPECIALISTS:\nbc-planner\n"
           f"FOLDER:\n{_ROOTS[0]}\n")
    prompt, stages = card_prepare.parse(raw, roster=_ROSTER)
    assert prompt == "Fix the strip wrap."
    assert stages == ["bc-planner"]


def test_a_legacy_answer_that_mentions_folder_parses_exactly_as_before():
    raw = ("INSTRUCTIONS:\nRename the FOLDER: label in the docs.\n"
           "SPECIALISTS:\nNONE\n")
    assert card_prepare.parse(raw, roster=_ROSTER) == (
        "Rename the FOLDER: label in the docs.", [])


def _prepare_daemon(tmp_path, monkeypatch, answer: bytes):
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    monkeypatch.setattr(agents_poll, "find_claude_binary",
                        lambda: "/bin/claude")
    monkeypatch.setattr(card_prepare, "read_roster", lambda _root: [])

    async def fake_exec(*args, **kwargs):
        fake_exec.argv = list(args)
        return _FakeProc(answer)

    fake_exec.argv = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return daemon, fake_exec


@pytest.mark.asyncio
async def test_the_locked_body_returns_the_suggested_root(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    daemon, _ = _prepare_daemon(
        tmp_path, monkeypatch,
        b"INSTRUCTIONS:\nDo it.\nSPECIALISTS:\nNONE\n"
        + f"FOLDER:\n{_ROOTS[1]}\n".encode())
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "t", "summary": "s", "tool": "claude", "project": "p"},
        str(root), roots=_ROOTS)
    assert result is not None, detail
    assert result["suggested_root"] == _ROOTS[1]
    assert result["prompt"] == "Do it."


@pytest.mark.asyncio
async def test_an_off_list_folder_still_returns_the_prompt(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    daemon, _ = _prepare_daemon(
        tmp_path, monkeypatch,
        b"INSTRUCTIONS:\nDo it.\nSPECIALISTS:\nNONE\nFOLDER:\n/not/offered\n")
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "t", "summary": "s", "tool": "claude", "project": "p"},
        str(root), roots=_ROOTS)
    assert result is not None, detail
    assert result["prompt"] == "Do it."
    assert result["suggested_root"] == ""


@pytest.mark.asyncio
async def test_the_locked_body_still_takes_two_positional_arguments(
        tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    daemon, fake_exec = _prepare_daemon(
        tmp_path, monkeypatch,
        b"INSTRUCTIONS:\nDo it.\nSPECIALISTS:\nNONE\n")
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "t", "summary": "s", "tool": "claude", "project": "p"},
        str(root))
    assert result is not None, detail
    assert result["suggested_root"] == ""
    prompt = fake_exec.argv[fake_exec.argv.index("-p") + 1]
    assert "FOLDER:" not in prompt


@pytest.mark.asyncio
async def test_a_refused_prompt_never_carries_a_suggestion(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    daemon, _ = _prepare_daemon(
        tmp_path, monkeypatch,
        b"SPECIALISTS:\nNONE\n" + f"FOLDER:\n{_ROOTS[0]}\n".encode())
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "t", "summary": "s", "tool": "claude", "project": "p"},
        str(root), roots=_ROOTS)
    assert result is None
    assert detail


@pytest.mark.asyncio
async def test_the_offer_is_the_intersection_not_every_known_root(
        tmp_path, monkeypatch):
    """`_known_project_roots` also holds live-session cwds with no open
    window; a suggestion naming one of those is unselectable in either
    composer, so it is never offered."""
    root = tmp_path / "project"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    windowless = tmp_path / "windowless"
    windowless.mkdir()
    daemon, fake_exec = _prepare_daemon(
        tmp_path, monkeypatch,
        b"INSTRUCTIONS:\nDo it.\nSPECIALISTS:\nNONE\n"
        + f"FOLDER:\n{windowless}\n".encode())
    daemon._known_project_roots = lambda: [
        str(root), str(other), str(windowless)]
    daemon._board_state = {"projects": [{"name": "project", "root": str(root)},
                                        {"name": "other", "root": str(other)}]}
    result, detail = await daemon.prepare_card_text(
        {"summary": "s", "tool": "claude", "project": "p", "root": str(root)})
    assert result is not None, detail
    assert result["suggested_root"] == ""
    argv = " ".join(fake_exec.argv)
    assert str(root) in argv and str(other) in argv
    assert str(windowless) not in argv


# --- The daemon resolves the card's own assistant


@pytest.mark.asyncio
async def test_a_codex_card_runs_codex_from_the_daemon(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    monkeypatch.setattr(agents_poll, "find_claude_binary",
                        lambda: "/bin/claude")
    monkeypatch.setattr(
        dispatch, "resolve_executable",
        lambda tool: {"codex": "/opt/codex", "claude": "/bin/claude"}.get(tool))
    monkeypatch.setattr(card_prepare, "read_roster", lambda _root: [])
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["argv"] = list(args)
        seen["kwargs"] = kwargs
        return _FakeProc(
            b"INSTRUCTIONS:\nFix the usage wrap.\n\nSPECIALISTS:\nNONE\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "t", "summary": "s", "tool": "codex", "project": "p",
         "attachment_paths": []}, str(root))
    assert result is not None, detail
    assert result["prompt"] == "Fix the usage wrap."
    assert seen["argv"][:2] == ["/opt/codex", "exec"]
    assert seen["argv"][seen["argv"].index("--model") + 1] == (
        card_prepare.HELPER_MODELS["codex"])
    assert seen["kwargs"]["cwd"] == str(root)
    assert seen["kwargs"]["stdin"] == asyncio.subprocess.DEVNULL
    env = seen["kwargs"]["env"]
    assert env["BOB_COMPANION_PORT"] == card_prepare.QUIET_HOOK_PORT
    assert env["DARK_ARMY_HOOK_SOCKET"] == card_prepare.QUIET_HOOK_SOCKET


@pytest.mark.asyncio
async def test_a_claude_card_still_inherits_the_daemon_environment(
        tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    monkeypatch.setattr(agents_poll, "find_claude_binary",
                        lambda: "/bin/claude")
    monkeypatch.setattr(card_prepare, "read_roster", lambda _root: [])
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["argv"] = list(args)
        seen["kwargs"] = kwargs
        return _FakeProc(
            b"INSTRUCTIONS:\nFix the usage wrap.\n\nSPECIALISTS:\nNONE\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "t", "summary": "s", "tool": "claude", "project": "p",
         "attachment_paths": []}, str(root))
    assert result is not None, detail
    assert seen["argv"][0] == "/bin/claude"
    assert seen["kwargs"]["env"] is not None
    assert "PYTHONHOME" not in seen["kwargs"]["env"]


@pytest.mark.asyncio
async def test_a_missing_grok_binary_is_refused_in_groks_name(
        tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    monkeypatch.setattr(agents_poll, "find_claude_binary",
                        lambda: "/bin/claude")
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: None)
    monkeypatch.setattr(card_prepare, "read_roster", lambda _root: [])

    async def fake_exec(*args, **kwargs):  # pragma: no cover
        raise AssertionError("nothing may be started")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "t", "summary": "s", "tool": "grok", "project": "p",
         "attachment_paths": []}, str(root))
    assert result is None
    assert detail == ("grok is not installed on this Mac "
                      "(Dark Army looked on PATH and at its usual install sites)")


@pytest.mark.asyncio
async def test_prepare_refuses_attachments_on_a_codex_card(
        tmp_path, monkeypatch):
    """Attachments live under ~/.dark-army; codex exec's read-only
    sandbox is the project. Succeeding here would look like Prepare worked
    and ignore the photos."""
    root = tmp_path / "project"
    root.mkdir()
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    monkeypatch.setattr(
        dispatch, "resolve_executable",
        lambda tool: {"codex": "/opt/codex", "claude": "/bin/claude"}.get(tool))
    monkeypatch.setattr(card_prepare, "read_roster", lambda _root: [])

    async def fake_exec(*args, **kwargs):  # pragma: no cover
        raise AssertionError("nothing may be started")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "t", "summary": "s", "tool": "codex", "project": "p",
         "attachment_paths": ["/tmp/shot.png"]}, str(root))
    assert result is None
    assert "cannot read attached files" in detail
    assert "Claude" in detail


# --- the three brief routes ------------------------------------------------


def test_claude_argv_carries_the_inline_agent():
    args = _argv()
    i = args.index("--agents")
    payload = json.loads(args[i + 1])
    assert list(payload) == [card_preparer_brief.NAME]
    agent = payload[card_preparer_brief.NAME]
    assert set(agent) == {"description", "prompt"}
    assert agent["description"] == card_preparer_brief.DESCRIPTION
    assert agent["prompt"] == card_preparer_brief.BRIEF
    assert args[args.index("--agent") + 1] == card_preparer_brief.NAME
    assert args[1:9] == [
        "-p", args[2], "--model", card_prepare.MODEL,
        "--no-session-persistence", "--strict-mcp-config",
        "--setting-sources", "",
    ]


def test_claude_argv_with_attachments_swaps_the_brief_and_keeps_the_tail():
    paths = ("/tmp/a.png", "/tmp/b.pdf")
    args = _argv(attachments=paths)
    payload = json.loads(args[args.index("--agents") + 1])
    prompt = payload[card_preparer_brief.NAME]["prompt"]
    assert card_prepare._NO_TOOLS not in prompt
    assert card_prepare._WITH_ATTACHMENTS in prompt
    i = args.index("--allowed-tools")
    assert args[i + 1:] == [f"Read(//{p})" for p in paths]


def test_grok_argv_without_brief_path_raises():
    with pytest.raises(ValueError, match="brief_path"):
        card_prepare.argv("/opt/grok", title="t", summary="s",
                          tool="grok", project="p")


def test_agents_json_round_trips():
    raw = card_prepare.agents_json(
        "bc-card-preparer", "drafts a card", "the brief")
    assert json.loads(raw) == {
        "bc-card-preparer": {
            "description": "drafts a card",
            "prompt": "the brief",
        }
    }


def test_grok_agent_file_starts_with_frontmatter_and_ends_with_the_body():
    body = card_preparer_brief.BRIEF
    text = card_prepare.grok_agent_file(
        "bc-card-preparer", 'says "hello"', body)
    assert text.startswith("---\nname: ")
    assert text.endswith(body)
    assert 'description: "says \'hello\'"' in text
    assert "prompt_mode: full" in text


def test_argv_empty_idea_and_empty_roots_stay_byte_identical():
    assert card_prepare.argv("claude", title="t", summary="s",
                             tool="claude", project="p", idea="") == _argv()
    assert card_prepare.argv("claude", title="t", summary="s",
                             tool="claude", project="p", roots=()) == _argv()


@pytest.mark.asyncio
async def test_a_grok_press_writes_the_agent_file(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    daemon, fake_exec = _prepare_daemon(
        tmp_path, monkeypatch,
        b"INSTRUCTIONS:\nDo it.\nSPECIALISTS:\nNONE\n")
    monkeypatch.setattr(
        dispatch, "resolve_executable",
        lambda tool: {"grok": "/opt/grok", "claude": "/bin/claude"}.get(tool))
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "t", "summary": "s", "tool": "grok", "project": "p",
         "attachment_paths": []}, str(root))
    assert result is not None, detail
    written = paths.ensure_state_dir() / card_prepare.GROK_AGENT_FILENAME
    assert written.is_file()
    assert fake_exec.argv[fake_exec.argv.index("--agent") + 1] == str(written)
    assert written.read_text(encoding="utf-8").endswith(
        card_preparer_brief.BRIEF)


@pytest.mark.asyncio
async def test_a_claude_press_argv_carries_agents(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    daemon, fake_exec = _prepare_daemon(
        tmp_path, monkeypatch,
        b"INSTRUCTIONS:\nDo it.\nSPECIALISTS:\nNONE\n")
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "t", "summary": "s", "tool": "claude", "project": "p",
         "attachment_paths": []}, str(root))
    assert result is not None, detail
    assert "--agents" in fake_exec.argv
    payload = json.loads(fake_exec.argv[fake_exec.argv.index("--agents") + 1])
    assert card_preparer_brief.NAME in payload


@pytest.mark.asyncio
async def test_the_projects_own_brief_reaches_argv(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    custom = card_preparer_brief.BRIEF.replace(
        "no more than 14 words", "no more than 14 words (custom)")
    _preparer_file(root / ".claude" / "agents", body=custom)
    daemon, fake_exec = _prepare_daemon(
        tmp_path, monkeypatch,
        b"INSTRUCTIONS:\nDo it.\nSPECIALISTS:\nNONE\n")
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "t", "summary": "s", "tool": "claude", "project": "p",
         "attachment_paths": []}, str(root))
    assert result is not None, detail
    payload = json.loads(fake_exec.argv[fake_exec.argv.index("--agents") + 1])
    assert "(custom)" in payload[card_preparer_brief.NAME]["prompt"]


# --- the objective, drafted with the card ----------------------------------

_SEVEN = (
    "TITLE: Stop the strip wrapping\n"
    "SUMMARY: The menu bar wraps when three agents run.\n"
    "BENEFICIARY:\n- \"Ops\"\n"
    "BENEFIT: fewer pages at night\n"
    "CRITERION: No page in a week.\n"
    "INSTRUCTIONS: Measure the strip and step the ladder down.\n"
    "SPECIALISTS:\nbc-planner\n"
)


def test_parse_objective_reads_the_three_and_cleans_each_line():
    assert card_prepare.parse_objective(_SEVEN) == {
        "beneficiary": "Ops",
        "intended_benefit": "fewer pages at night",
        "success_criterion": "No page in a week.",
    }


def test_parse_objective_reads_an_absent_label_and_none_as_empty():
    got = card_prepare.parse_objective(
        "TITLE: t\nSUMMARY: s\nCRITERION: NONE\nINSTRUCTIONS: Do it.\n"
        "SPECIALISTS:\nNONE\n")
    assert got == {"beneficiary": "", "intended_benefit": "",
                   "success_criterion": ""}
    assert card_prepare.parse_objective("") == got


def test_parse_objective_survives_a_fenced_answer():
    assert card_prepare.parse_objective("```\n" + _SEVEN + "```")[
        "beneficiary"] == "Ops"


def test_parse_idea_on_a_seven_section_answer_returns_the_same_four_values():
    """The objective sections are cut out cleanly: the summary stops where
    BENEFICIARY: starts, the instructions still end at SPECIALISTS:."""
    title, summary, prompt, stages = card_prepare.parse_idea(
        _SEVEN, roster=_ROSTER)
    assert title == "Stop the strip wrapping"
    assert summary == "The menu bar wraps when three agents run."
    assert prompt == "Measure the strip and step the ladder down."
    assert stages == ["bc-planner"]


def test_objective_refusal_rejects_over_long_lines_in_words_not_by_trimming():
    from dark_army_daemon import board_outcomes
    cap = board_outcomes.OBJECTIVE_LIMITS["success_criterion"]
    reason = card_prepare.objective_refusal({"success_criterion": "x" * (cap + 1)})
    assert reason and "success criterion" in reason and str(cap) in reason
    assert card_prepare.objective_refusal(
        {"success_criterion": "x" * cap}) is None
    cap = board_outcomes.OBJECTIVE_LIMITS["beneficiary"]
    reason = card_prepare.objective_refusal({"beneficiary": "b" * (cap + 1)})
    assert reason and "who benefits" in reason
    cap = board_outcomes.OBJECTIVE_LIMITS["intended_benefit"]
    assert card_prepare.objective_refusal(
        {"intended_benefit": "i" * (cap + 1)})


@pytest.mark.parametrize("fields", [
    {}, {"beneficiary": ""}, {"beneficiary": "", "intended_benefit": "",
                              "success_criterion": ""}, None,
])
def test_objective_refusal_accepts_empties(fields):
    assert card_prepare.objective_refusal(fields) is None


@pytest.mark.asyncio
async def test_an_idea_press_answers_with_the_three_objective_keys(
        tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    daemon, _ = _prepare_daemon(tmp_path, monkeypatch, _SEVEN.encode())
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "", "summary": "", "tool": "claude", "project": "p",
         "idea": "stop the strip wrapping"},
        str(root))
    assert result is not None, detail
    assert result["title"] == "Stop the strip wrapping"
    assert result["beneficiary"] == "Ops"
    assert result["intended_benefit"] == "fewer pages at night"
    assert result["success_criterion"] == "No page in a week."


@pytest.mark.asyncio
async def test_an_idea_press_with_no_objective_answers_empty_not_a_refusal(
        tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    daemon, _ = _prepare_daemon(
        tmp_path, monkeypatch,
        b"TITLE: A name\nSUMMARY: One sentence.\nCRITERION: NONE\n"
        b"INSTRUCTIONS: Do it.\nSPECIALISTS:\nNONE\n")
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "", "summary": "", "tool": "claude", "project": "p",
         "idea": "an idea"},
        str(root))
    assert result is not None, detail
    assert (result["beneficiary"], result["intended_benefit"],
            result["success_criterion"]) == ("", "", "")


@pytest.mark.asyncio
async def test_an_over_long_criterion_refuses_the_whole_press(
        tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    daemon, _ = _prepare_daemon(
        tmp_path, monkeypatch,
        b"TITLE: A name\nSUMMARY: One sentence.\nCRITERION: "
        + b"x" * 1001
        + b"\nINSTRUCTIONS: Do it.\nSPECIALISTS:\nNONE\n")
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "", "summary": "", "tool": "claude", "project": "p",
         "idea": "an idea"},
        str(root))
    assert result is None
    assert "success criterion" in detail


@pytest.mark.parametrize("raw,want", [
    ("AREA:\nBackbone", "backbone"), ("AREA:\npocket", "pocket"),
    ("AREA:\nNONE", ""), ("no area", ""), ("AREA:\ninvalid", ""),
    ("AREA:\nBackbone or Desk", ""),
    ("AREA:\nGate\nAREA:\nDesk", "desk"),
    # Decoration and a tacked-on description are read past; "no opinion"
    # stays `""` — the fallback to Universal is the daemon's, not this
    # function's.
    ("AREA: **Pocket** — phones & widgets", "pocket"),
    ("**AREA:** Pocket", "pocket"),
    ("## AREA\nPocket", "pocket"),
    ("AREA: Pocket (phones & widgets)", "pocket"),
    ("AREA: `Pocket` - phones", "pocket"),
    ("AREA: NONE", ""),
])
def test_parse_area(raw, want):
    assert card_prepare.parse_area(raw) == want

def test_area_after_folder_keeps_both_suggestions_and_stages():
    raw = "INSTRUCTIONS:\nBuild this thing carefully.\nSPECIALISTS:\nbc-planner\nFOLDER:\n/tmp/app\nAREA:\nBackbone"
    assert card_prepare.parse_folder(raw, ["/tmp/app", "/tmp/other"]) == "/tmp/app"
    assert card_prepare.parse_area(raw) == "backbone"
    assert card_prepare.parse(raw, roster=_ROSTER)[1] == ["bc-planner"]
    prompt = card_prepare.prompt_for(title="x",summary="x",tool="claude",project="x",roots=["/tmp/app","/tmp/other"])
    assert prompt.index("AREA:") > prompt.index("FOLDER:")


# --- Prepare tolerates a decorated answer, logs a refused one, falls back ---
# --- to Universal (plans/2026-09-20-prepare-tolerant-parse-universal-area) ---


def test_parse_idea_reads_bold_labels():
    title, summary, prompt, _st = card_prepare.parse_idea(
        "**TITLE:** Foo bar\n**SUMMARY:** Baz.\n**INSTRUCTIONS:** Do it.\n"
        "**SPECIALISTS:** NONE")
    assert (title, summary, prompt) == ("Foo bar", "Baz.", "Do it.")


def test_parse_idea_reads_heading_labels_without_colons():
    title, summary, prompt, _st = card_prepare.parse_idea(
        "## TITLE\nFoo bar\n## SUMMARY\nBaz.\n## INSTRUCTIONS\nDo it.\n"
        "## SPECIALISTS\nNONE")
    assert (title, summary, prompt) == ("Foo bar", "Baz.", "Do it.")


@pytest.mark.parametrize("head", [
    "`TITLE:` Foo", "TITLE: **Foo**", "TITLE: `Foo`", "TITLE: **`Foo`**",
    "__TITLE:__ Foo", "TITLE: _Foo_",
])
def test_parse_idea_strips_matched_decoration_off_the_title(head):
    title, _s, _p, _st = card_prepare.parse_idea(
        head + "\nSUMMARY: s\nINSTRUCTIONS: Do it.\nSPECIALISTS: NONE")
    assert title == "Foo"


@pytest.mark.parametrize("head", [
    "**Foo", "`a` and `b`", "*a* and *b*", "_init_ and _exit_",
    "`board.py` and `daemon.py`", "*Fix* the *strip*",
])
def test_parse_idea_keeps_an_unmatched_wrapper(head):
    """`**Foo` alone is a typo, not decoration, and two inline spans that
    happen to open and close the line are not one wrapper; stripping one
    side of either would silently rewrite the title."""
    title, _s, _p, _st = card_prepare.parse_idea(
        "TITLE: " + head
        + "\nSUMMARY: s\nINSTRUCTIONS: Do it.\nSPECIALISTS: NONE")
    assert title == head


def test_parse_idea_leaves_a_wrapper_glued_to_the_colon_to_the_answer():
    """`TITLE:*Foo*` — the star opens the answer's own span, so the label
    match must not eat it and leave `Foo*`."""
    title, _s, _p, _st = card_prepare.parse_idea(
        "TITLE:*Foo*\nSUMMARY: s\nINSTRUCTIONS: Do it.\nSPECIALISTS: NONE")
    assert title == "Foo"
    # A wrapper closing the label and glued to the answer still partitions,
    # the unmatched remainder kept whole for the refusals to judge.
    title, _s, _p, _st = card_prepare.parse_idea(
        "**TITLE:**Foo\nSUMMARY: s\nINSTRUCTIONS: Do it.\nSPECIALISTS: NONE")
    assert title == "**Foo"


def test_parse_idea_does_not_partition_on_a_label_mid_sentence():
    body = "Write the SUMMARY of the work at the top, then the rest."
    _t, _s, prompt, _st = card_prepare.parse_idea(
        "TITLE: t\nSUMMARY: s\nINSTRUCTIONS: " + body + "\nSPECIALISTS: NONE")
    assert prompt == body


def test_parse_idea_still_refuses_a_missing_long_or_multiline_title():
    """The refusal wording and bounds are untouched by the tolerant read."""
    title, _s, _p, _st = card_prepare.parse_idea(
        "SUMMARY: s\nINSTRUCTIONS: Do it.\nSPECIALISTS: NONE")
    assert card_prepare.title_refusal(title) == (
        "Dark Army did not write a title — press Prepare again, or type one")
    long_title = " ".join(["word"] * (card_prepare.MAX_TITLE_WORDS + 1))
    assert card_prepare.title_refusal(long_title)
    title, _s, _p, _st = card_prepare.parse_idea(
        "TITLE:\nFirst line\nSecond line\nSUMMARY: s\n"
        "INSTRUCTIONS: Do it.\nSPECIALISTS: NONE")
    assert title == "First line"


def test_parse_objective_reads_a_bold_label():
    got = card_prepare.parse_objective(
        "**BENEFICIARY:** People\nINSTRUCTIONS: Do it.")
    assert got["beneficiary"] == "People"


def test_log_excerpt_collapses_whitespace_and_bounds_the_length():
    assert card_prepare.log_excerpt("a\nb\tc") == "a b c"
    cut = card_prepare.log_excerpt("x" * 700)
    assert cut == "x" * card_prepare.LOG_EXCERPT_CHARS + "…"
    assert card_prepare.LOG_EXCERPT_CHARS == 600
    assert card_prepare.log_excerpt("") == ""
    assert card_prepare.log_excerpt(None) == ""


def test_the_legacy_parser_is_not_widened_to_decorated_labels():
    """`parse` keeps its literal `INSTRUCTIONS:` rule byte for byte: a bold
    label leaves its trailing stars in the prompt, exactly as today."""
    assert card_prepare.parse(
        "**INSTRUCTIONS:** Do it.\nSPECIALISTS: NONE")[0] == "** Do it."


def _tolerant_daemon(tmp_path, monkeypatch, stdout: bytes):
    """`_prepare_daemon` plus a project root on disk."""
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    daemon, _ = _prepare_daemon(tmp_path, monkeypatch, stdout)
    return daemon, root


_IDEA_FIELDS = {"title": "", "summary": "", "tool": "claude", "project": "p",
                "idea": "an idea"}


@pytest.mark.asyncio
async def test_an_idea_press_reads_bold_labels_and_falls_back_to_universal(
        tmp_path, monkeypatch):
    daemon, root = _tolerant_daemon(
        tmp_path, monkeypatch,
        b"**TITLE:** Foo bar\n**SUMMARY:** Baz.\n**INSTRUCTIONS:** Do it.\n"
        b"**SPECIALISTS:** NONE\nAREA: NONE\n")
    result, detail = await daemon._prepare_card_text_locked(
        dict(_IDEA_FIELDS), str(root))
    assert result is not None, detail
    assert result["title"] == "Foo bar"
    assert result["summary"] == "Baz."
    assert result["prompt"] == "Do it."
    assert result["suggested_area"] == "universal"


@pytest.mark.asyncio
async def test_a_legacy_press_without_an_area_falls_back_to_universal(
        tmp_path, monkeypatch):
    daemon, root = _tolerant_daemon(
        tmp_path, monkeypatch,
        b"INSTRUCTIONS:\nDo the thing.\nSPECIALISTS: NONE\n")
    result, detail = await daemon._prepare_card_text_locked(
        {"title": "t", "summary": "s", "tool": "claude", "project": "p",
         "attachment_paths": []},
        str(root))
    assert result is not None, detail
    assert result["suggested_area"] == "universal"
    assert set(result) == {"prompt", "workflow", "suggested_root",
                           "suggested_area"}


@pytest.mark.asyncio
async def test_a_decorated_area_answer_is_not_masked_by_the_fallback(
        tmp_path, monkeypatch):
    daemon, root = _tolerant_daemon(
        tmp_path, monkeypatch,
        b"TITLE: Foo bar\nSUMMARY: Baz.\nINSTRUCTIONS: Do it.\n"
        b"SPECIALISTS: NONE\nAREA: **Pocket** \xe2\x80\x94 phones & widgets\n")
    result, detail = await daemon._prepare_card_text_locked(
        dict(_IDEA_FIELDS), str(root))
    assert result is not None, detail
    assert result["suggested_area"] == "pocket"


@pytest.mark.asyncio
async def test_a_refused_title_logs_the_reason_and_an_excerpt(
        tmp_path, monkeypatch, caplog):
    daemon, root = _tolerant_daemon(
        tmp_path, monkeypatch,
        b"TITLE:\nSUMMARY: s\nINSTRUCTIONS: Do it.\nSPECIALISTS: NONE")
    with caplog.at_level(logging.INFO, logger="dark-army"):
        result, detail = await daemon._prepare_card_text_locked(
            dict(_IDEA_FIELDS), str(root))
    assert result is None
    records = [r.getMessage() for r in caplog.records
               if r.getMessage().startswith("Prepare refused")]
    assert len(records) == 1
    assert records[0].startswith(
        "Prepare refused (Dark Army did not write a title")
    assert "helper answer: TITLE: SUMMARY: s" in records[0]


@pytest.mark.asyncio
async def test_a_successful_press_logs_no_refusal(
        tmp_path, monkeypatch, caplog):
    daemon, root = _tolerant_daemon(
        tmp_path, monkeypatch,
        b"TITLE: Foo bar\nSUMMARY: Baz.\nINSTRUCTIONS: Do it.\n"
        b"SPECIALISTS: NONE\nAREA: Pocket\n")
    with caplog.at_level(logging.INFO, logger="dark-army"):
        result, detail = await daemon._prepare_card_text_locked(
            dict(_IDEA_FIELDS), str(root))
    assert result is not None, detail
    assert not [r for r in caplog.records
                if "Prepare refused" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_refused_legacy_prompt_logs_the_reason(
        tmp_path, monkeypatch, caplog):
    daemon, root = _tolerant_daemon(
        tmp_path, monkeypatch, b"INSTRUCTIONS:\n\nSPECIALISTS: NONE")
    with caplog.at_level(logging.INFO, logger="dark-army"):
        result, _detail = await daemon._prepare_card_text_locked(
            {"title": "t", "summary": "s", "tool": "claude", "project": "p",
             "attachment_paths": []},
            str(root))
    assert result is None
    records = [r.getMessage() for r in caplog.records
               if r.getMessage().startswith("Prepare refused")]
    assert len(records) == 1
    assert records[0].startswith(
        "Prepare refused (Dark Army's answer was not usable")


@pytest.mark.asyncio
async def test_a_refused_objective_logs_the_reason(
        tmp_path, monkeypatch, caplog):
    daemon, root = _tolerant_daemon(
        tmp_path, monkeypatch,
        b"TITLE: Foo bar\nSUMMARY: Baz.\nBENEFICIARY: " + b"x" * 201
        + b"\nINSTRUCTIONS: Do it.\nSPECIALISTS: NONE")
    with caplog.at_level(logging.INFO, logger="dark-army"):
        result, _detail = await daemon._prepare_card_text_locked(
            dict(_IDEA_FIELDS), str(root))
    assert result is None
    records = [r.getMessage() for r in caplog.records
               if r.getMessage().startswith("Prepare refused")]
    assert len(records) == 1
