"""`close-out.sh --plan`: a planning run closes its own tab, nothing else does.

The person asked for the old rule back on 23 Sep 2026: a planning run's whole
output is the plan on the card, and leaving every planning tab open is how a
day ended with a dozen of them. The close is decided off Dark Army's own board
(a card naming this session as `refine_session_id`), for Claude, Codex and
Grok alike, so an implementation or scout run that ran `--plan` stays open.
"""
import json

import pytest

from tests.test_ship_close_out import (  # noqa: F401  (fixtures used by name)
    CODEX_ID, CODEX_SID, SESSION_ID, _EVERY_REPLY, _Stub, _codex_rows, _run,
    home, script,
)

PLAN = ("--plan",)


def _state(sid, rows, refining):
    cards = [{"id": "c1", "column_name": "backlog",
              "refine_session_id": sid if refining else "someone-else"}]
    return json.dumps({"agents": rows, "board": {"cards": cards}}).encode()


def _case(provider, refining):
    import os
    if provider == "claude":
        rows = {"running": [{"session_id": SESSION_ID, "pid": os.getpid()}]}
        return {}, _state(SESSION_ID, rows, refining), "close_terminal", SESSION_ID
    if provider == "grok":
        return ({"GROK_SESSION_ID": "grok-sess-1"},
                _state("grok-sess-1", {"running": []}, refining),
                "close_terminal", "grok-sess-1")
    return ({"CODEX_THREAD_ID": CODEX_ID},
            _state(CODEX_SID, _codex_rows(can_close=True), refining),
            "close_refinement_terminal", CODEX_SID)


@pytest.mark.parametrize("provider", ["claude", "grok", "codex"])
def test_a_planning_run_closes_its_own_tab(script, home, provider):
    env, raw, action, sid = _case(provider, refining=True)
    stub = _Stub(dict(_EVERY_REPLY), raw_state=raw)
    try:
        result = _run(script, home, stub.port, env, args=PLAN)
    finally:
        stub.close()
    assert result.returncode == 0
    assert stub.actions == [action]
    assert stub.session_ids == [sid]
    assert result.stdout.strip() == "close-out: closed the terminal."


@pytest.mark.parametrize("provider", ["claude", "grok", "codex"])
def test_a_run_that_is_not_planning_stays_open(script, home, provider):
    env, raw, _action, _sid = _case(provider, refining=False)
    stub = _Stub(dict(_EVERY_REPLY), raw_state=raw)
    try:
        result = _run(script, home, stub.port, env, args=PLAN)
    finally:
        stub.close()
    assert stub.actions == []
    assert "terminal left open" in result.stdout
    assert "not a card's planning run" in result.stdout


def test_an_installed_copy_without_the_plan_marker_is_not_handed_the_job(
        script, home):
    """An installed helper from before `--plan` would call it unknown."""
    old = home / ".dark-army" / "dark-army-close-out"
    old.write_text("#!/bin/bash\n# close-out-contract: leaves-open\n"
                   "echo old-helper\n")
    old.chmod(0o755)
    env, raw, action, _sid = _case("claude", refining=True)
    stub = _Stub(dict(_EVERY_REPLY), raw_state=raw)
    try:
        result = _run(script, home, stub.port, env, args=PLAN)
    finally:
        stub.close()
    assert "old-helper" not in result.stdout
    assert stub.actions == [action]


def test_the_plan_reference_ends_on_the_plan_close():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for ref in (root / ".claude/skills/ship/references/plan.md",
                root / "host/dark_army_menubar/agent_pack/template/.claude/skills/ship/references/plan.md"):
        text = ref.read_text()
        phase = text.split("## Phase 5b: close out", 1)[1]
        assert "close-out.sh --plan" in phase, ref
        assert "very last act" in phase, ref
