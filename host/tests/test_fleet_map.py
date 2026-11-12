"""The fleet activity map's host rules: the station table, the end-inference
rule, the section's shape and privacy, and the loopback-only seam."""

import inspect
import json
import re
from pathlib import Path

import pytest

from dark_army_daemon import api_server as api_mod
from dark_army_daemon import daemon as daemon_mod
from dark_army_daemon import fleet_map
from dark_army_daemon import relay_ws
from dark_army_daemon.daemon import BobDaemon, TOOL_ANIMATION_MAP


def _ring(*steps):
    """A ring after `steps`, each `(event, hook, tool)`."""
    ring = fleet_map.CallRing()
    for seq, (event, hook, tool) in enumerate(steps, 1):
        fleet_map.advance(ring, event, hook, tool, seq)
    return ring


def _ends(ring):
    return [(c["station"], c["end"]) for c in ring]


# --- station_for -------------------------------------------------------------

@pytest.mark.parametrize("tool, station", [
    ("Read", "files"), ("Edit", "files"), ("Glob", "files"), ("list_dir", "files"),
    ("Bash", "shell"), ("run_terminal_command", "shell"),
    ("WebSearch", "web"), ("web_fetch", "web"),
    ("Agent", "helpers"), ("Task", "helpers"), ("spawn_subagent", "helpers"),
    ("Skill", "skills"), ("SlashCommand", "skills"),
    ("AskUserQuestion", "you"), ("ask_user_question", "you"),
    ("mcp__x__y", "tools"), ("gitnexus__query", "tools"), ("LSP", "tools"),
    ("SomethingNew", "tools"), ("", "tools"), (None, "tools"),
])
def test_station_for_table(tool, station):
    assert fleet_map.station_for(tool) == station


def test_every_animation_tool_has_a_station():
    for tool in TOOL_ANIMATION_MAP:
        assert fleet_map.station_for(tool) in fleet_map.STATION_IDS


def test_seven_stations_in_order():
    assert [i for i, _ in fleet_map.STATIONS] == [
        "you", "files", "shell", "web", "helpers", "skills", "tools"]


# --- advance: the end-inference rule -----------------------------------------

def test_tool_use_opens_a_running_call():
    ring = _ring(("tool_use", "PreToolUse", "Read"))
    assert _ends(ring) == [("files", "running")]


def test_a_new_call_closes_the_open_one_as_done():
    ring = _ring(("tool_use", "", "Read"), ("tool_use", "", "Bash"))
    assert _ends(ring) == [("files", "done"), ("shell", "running")]


@pytest.mark.parametrize("event, hook", [
    ("tool_done", "PostToolUse"), ("add", "Stop"), ("add", "StopFailure"),
    ("add", "Notification"), ("dismiss", "UserPromptSubmit"),
    ("dismiss", "SessionEnd"),
])
def test_ending_events_close_the_open_call_as_done(event, hook):
    ring = _ring(("tool_use", "", "Read"), (event, hook, ""))
    assert _ends(ring) == [("files", "done")]


def test_tool_failed_marks_failed_and_steps_errors():
    ring = _ring(("tool_use", "", "Bash"), ("tool_failed", "", "Bash"))
    assert _ends(ring) == [("shell", "failed")]
    assert ring.errors == 1


def test_errors_reset_on_the_next_prompt_only():
    ring = _ring(("tool_use", "", "Bash"), ("tool_failed", "", "Bash"))
    fleet_map.advance(ring, "dismiss", "SessionEnd", "", 9)
    assert ring.errors == 1
    fleet_map.advance(ring, "dismiss", "UserPromptSubmit", "", 10)
    assert ring.errors == 0


def test_a_failure_outlives_the_ring_and_the_next_prompt_clears_it():
    ring = _ring(("tool_use", "", "Bash"), ("tool_failed", "", "Bash"))
    for _ in range(fleet_map.MAX_CALLS_PER_SESSION + 3):
        fleet_map.advance(ring, "tool_use", "", "Read", 50)
    assert not any(c["end"] == "failed" for c in ring)
    section = fleet_map.compose({"s": ring}, {})
    assert section["sessions"]["s"]["errors"] == 1
    assert section["sessions"]["s"]["errors_by_station"] == {"shell": 1}
    fleet_map.advance(ring, "dismiss", "UserPromptSubmit", "", 99)
    section = fleet_map.compose({"s": ring}, {})
    assert section["sessions"]["s"]["errors_by_station"] == {}
    assert section["sessions"]["s"]["errors"] == 0


def test_permission_parks_a_running_call_on_you_once():
    ring = _ring(("tool_use", "", "Bash"), ("permission", "", "Bash"))
    assert _ends(ring) == [("shell", "done"), ("you", "running")]
    assert fleet_map.advance(ring, "permission", "", "Bash", 7) is False
    assert len(ring) == 2


def test_a_failure_after_an_approval_is_charged_to_the_tool_not_to_you():
    ring = _ring(("tool_use", "", "Bash"), ("permission", "", "Bash"),
                 ("tool_failed", "", "Bash"))
    assert ring.errors_by_station == {"shell": 1}
    assert ring.parked == ""
    # An ask that fails outright is still You's: nothing was parked there.
    ring = _ring(("permission", "", "Bash"), ("tool_failed", "", "Bash"))
    assert ring.errors_by_station == {"you": 1}


def test_a_ring_without_session_state_leaves_the_section():
    daemon = BobDaemon.__new__(BobDaemon)
    daemon._fleet_calls = {"gone": fleet_map.CallRing(), "live": fleet_map.CallRing()}
    daemon._session_states = {"live": {}}
    daemon._agents_snapshot_cache = {}
    assert set(daemon.fleet_activity_snapshot()["sessions"]) == {"live"}
    assert set(daemon._fleet_calls) == {"live"}


def test_ask_tool_use_is_at_you_and_permission_does_not_double_it():
    ring = _ring(("tool_use", "", "AskUserQuestion"), ("permission", "", ""))
    assert _ends(ring) == [("you", "running")]


@pytest.mark.parametrize("event", ["session_start", "compact", "subagent_start"])
def test_other_events_change_nothing(event):
    ring = _ring(("tool_use", "", "Read"))
    assert fleet_map.advance(ring, event, "", "", 5) is False
    assert _ends(ring) == [("files", "running")]


def test_the_ring_is_capped():
    ring = _ring(*[("tool_use", "", "Read")] * 20)
    assert len(ring) == fleet_map.MAX_CALLS_PER_SESSION
    assert [c["seq"] for c in ring] == list(range(13, 21))


# --- compose -----------------------------------------------------------------

def test_two_sessions_at_files_compose_two_running_calls():
    rings = {"a": _ring(("tool_use", "", "Read")),
             "b": _ring(("tool_use", "", "Grep"))}
    rows = {"running": [
        {"session_id": "a", "stats": {"tool_counts": {"Read": 3, "Bash": 1}}},
        {"session_id": "b", "stats": {"tool_counts": {"Grep": 2}}},
    ], "waiting": [], "sleeping": []}
    section = fleet_map.compose(rings, rows)
    running_at_files = [
        sid for sid, s in section["sessions"].items()
        if any(c["station"] == "files" and c["end"] == "running"
               for c in s["calls"])]
    assert sorted(running_at_files) == ["a", "b"]
    assert section["sessions"]["a"]["station_counts"] == {"files": 3, "shell": 1}
    assert section["available"] is True
    assert [s["id"] for s in section["stations"]] == [
        i for i, _ in fleet_map.STATIONS]


def test_station_counts_ignore_garbage():
    assert fleet_map.station_counts(None) == {}
    assert fleet_map.station_counts({"Read": "x", "Bash": 0, "Edit": 2}) == {
        "files": 2}


def test_compose_carries_no_key_digest_path_or_clock():
    ring = _ring(("tool_use", "", "Bash"), ("tool_failed", "", "Bash"))
    section = fleet_map.compose({"s1": ring}, {"running": [
        {"session_id": "s1", "key": "SECRET", "cwd": "/Users/x/proj",
         "stats": {"tool_counts": {"Bash": 1}}}]})
    blob = json.dumps(section)
    for needle in ("key", "digest", "claim", "channel", "/", "SECRET"):
        assert needle not in blob
    assert not re.search(r'"(at|since|time\w*)"', blob)


# --- the daemon --------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_daemon_rings_follow_the_hook_stream():
    d = BobDaemon()
    await d._handle_message({"event": "tool_use", "session_id": "s1",
                             "tool_name": "Read"})
    await d._handle_message({"event": "tool_use", "session_id": "s2",
                             "tool_name": "Bash"})
    snap = d.fleet_activity_snapshot()
    assert set(snap["sessions"]) == {"s1", "s2"}
    assert snap["sessions"]["s1"]["calls"][-1]["station"] == "files"
    await d._handle_message({"event": "tool_failed", "session_id": "s2",
                             "tool_name": "Bash"})
    assert d.fleet_activity_snapshot()["sessions"]["s2"]["errors"] == 1


def test_forgetting_a_session_pops_its_ring():
    d = BobDaemon()
    d._note_fleet_call("tool_use", "", "s1", "Read")
    assert "s1" in d._fleet_calls
    d._forget_session("s1")
    assert "s1" not in d._fleet_calls


def test_a_session_start_alone_makes_no_ring():
    d = BobDaemon()
    d._note_fleet_call("session_start", "", "s1", "")
    assert d._fleet_calls == {}


def test_rings_never_reach_sessions_json():
    src = inspect.getsource(daemon_mod.BobDaemon)
    persisted = re.findall(r"def _persist_sessions.*?(?=\n    def )", src, re.S)
    assert all("_fleet_calls" not in block for block in persisted)


# --- the loopback-only seam --------------------------------------------------

def test_phone_picture_strips_the_section_and_keeps_the_rest():
    state = {"counts": {}, "fleet_activity": {"available": True}, "agents": {}}
    out = api_mod.phone_picture(state)
    assert "fleet_activity" not in out
    assert set(out) == {"counts", "agents"}
    assert "fleet_activity" in state


def test_fleet_activity_is_omittable_and_loopback_only():
    assert api_mod._OMITTABLE_SECTIONS[-1] == "fleet_activity"
    assert api_mod._LOOPBACK_ONLY_SECTIONS == ("fleet_activity",)


def test_the_relay_push_picture_goes_through_phone_picture():
    assert "phone_picture(self._api.state(" in inspect.getsource(relay_ws)


def test_a_phones_digests_are_unchanged_by_the_section():
    base = {"counts": {"a": 1}, "agents": {"b": 2}}
    with_fleet = dict(base, fleet_activity={"available": True})
    assert (api_mod._state_digests(api_mod.phone_picture(with_fleet))
            == api_mod._state_digests(base))


def test_hooks_py_is_untouched_by_the_map():
    hooks = Path(__file__).resolve().parent.parent / "dark_army_menubar" / "hooks.py"
    assert "fleet" not in hooks.read_text()
