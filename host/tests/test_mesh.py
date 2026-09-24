"""Who is talking to whom, and what Dark Army is honest about not being able to do.

Two halves: the transcript scanner noticing a `SendMessage`, and the daemon
joining that recipient name back to a live session. The join is the interesting
part — a name that resolves to nothing is kept, because an agent that has since
exited and a name that was simply wrong are both worth seeing.
"""
import json

from dark_army_daemon import session_stats as ss
from dark_army_daemon.daemon import BobDaemon


def _transcript(tmp_path, lines):
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines))
    return str(path)


def _send(to, ts="2026-08-16T10:00:00Z", extra=None):
    payload = {"to": to, "message": "ping"}
    if extra:
        payload.update(extra)
    return {"type": "assistant", "timestamp": ts,
            "message": {"model": "claude-opus-5", "content": [
                {"type": "tool_use", "name": "SendMessage", "input": payload}]}}


def test_a_send_becomes_an_edge_with_a_count(tmp_path):
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _send("hex-42", "2026-08-16T10:00:00Z"),
        _send("hex-42", "2026-08-16T10:05:00Z"),
        _send("mira-01", "2026-08-16T10:06:00Z"),
    ]))
    assert stats.sent_to["hex-42"]["count"] == 2
    assert stats.sent_to["hex-42"]["last"] == "2026-08-16T10:05:00Z"
    assert stats.sent_to["mira-01"]["count"] == 1
    # And it is still an ordinary tool call in the counts.
    assert stats.tool_counts["SendMessage"] == 3


def test_the_message_body_is_never_kept(tmp_path):
    """Who is talking to whom is a fact about the fleet. What they said is not
    ours, and this is the one place a transcript's prose could leak into a
    payload that goes to a UI."""
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _send("hex-42", extra={"message": "the api token is hunter2"}),
    ]))
    blob = json.dumps(stats.sent_to)
    assert "hunter2" not in blob


def test_a_send_with_no_recipient_is_not_an_edge(tmp_path):
    stats = ss.parse_transcript(_transcript(tmp_path, [
        {"type": "assistant", "timestamp": "2026-08-16T10:00:00Z",
         "message": {"model": "claude-opus-5", "content": [
             {"type": "tool_use", "name": "SendMessage",
              "input": {"message": "to nobody"}}]}},
    ]))
    assert stats.sent_to == {}


def test_the_edge_table_is_bounded(tmp_path):
    lines = [_send(f"agent-{i:03d}") for i in range(ss.MAX_MESH_EDGES + 20)]
    stats = ss.parse_transcript(_transcript(tmp_path, lines))
    assert len(stats.sent_to) == ss.MAX_MESH_EDGES


# ── the join ─────────────────────────────────────────────────────────────────

def _entry(sid, nickname, address="", addressable=False, sent_to=None):
    return {"session_id": sid, "nickname": nickname, "address": address,
            "addressable": addressable, "sent_to": sent_to or {}}


def test_an_edge_is_resolved_to_the_session_that_answers_to_the_name():
    snapshot = {"running": [
        _entry("a", "Vex", address="dark-army-6c", addressable=True,
               sent_to={"finance-demo-1a": {"count": 3, "last": "t"}}),
        _entry("b", "Relay", address="finance-demo-1a", addressable=True),
    ]}
    mesh = BobDaemon._build_mesh(snapshot)
    assert len(mesh) == 1
    edge = mesh[0]
    assert edge["from_nickname"] == "Vex"
    assert edge["to_session"] == "b" and edge["to_nickname"] == "Relay"
    assert edge["resolved"] is True and edge["reachable"] is True
    assert edge["count"] == 3


def test_a_live_session_with_no_inbox_is_resolved_but_not_reachable():
    """Three states, not two: a session that is present but deaf is not the same
    as one that has gone, and messaging it is how you would otherwise find out."""
    snapshot = {"running": [
        _entry("a", "Vex", sent_to={"deaf-one": {"count": 1}}),
        _entry("b", "Mira", address="deaf-one", addressable=False),
    ]}
    edge = BobDaemon._build_mesh(snapshot)[0]
    assert edge["resolved"] is True
    assert edge["reachable"] is False


def test_an_unresolvable_recipient_is_kept_rather_than_dropped():
    snapshot = {"running": [
        _entry("a", "Vex", sent_to={"ghost-99": {"count": 2}})]}
    edge = BobDaemon._build_mesh(snapshot)[0]
    assert edge["to_address"] == "ghost-99"
    assert edge["to_session"] == ""
    assert edge["resolved"] is False and edge["reachable"] is False


def test_edges_are_ordered_by_how_much_traffic_they_carry():
    snapshot = {"running": [
        _entry("a", "Vex", sent_to={"x": {"count": 1}, "y": {"count": 9}})]}
    mesh = BobDaemon._build_mesh(snapshot)
    assert [e["to_address"] for e in mesh] == ["y", "x"]


def test_a_fleet_that_never_messages_has_no_mesh():
    snapshot = {"running": [_entry("a", "Vex")], "sleeping": []}
    assert BobDaemon._build_mesh(snapshot) == []


def test_a_message_to_a_subagent_resolves_to_the_agent_and_its_owner():
    """Observed live before it was coded for: the first edge this ever drew was
    a session messaging somebody else's subagent by agent id, which resolves
    against no session name at all."""
    snapshot = {"running": [
        _entry("a", "Audit", sent_to={"a97d9a35": {"count": 1}}),
        dict(_entry("b", "Relay"), subagent_rows=[
            {"agent_id": "a97d9a35", "subagent_type": "sf-bug-auditor"}]),
    ]}
    edge = BobDaemon._build_mesh(snapshot)[0]
    assert edge["to_session"] == "b"
    assert edge["to_nickname"] == "sf-bug-auditor · Relay"
    assert edge["resolved"] is True
    # Reachable through the harness that spawned it, which is not something this
    # app can offer — so it does not claim to.
    assert edge["reachable"] is False


def test_a_recipient_typed_with_its_ref_still_resolves():
    """Found by sending one. `ListAgents` prints `name [ref]` and `SendMessage`
    accepts it, so that is what a transcript records — while the registry holds
    the bare name. Joined exactly, a live addressable peer in the very same
    snapshot came back as `resolved: false`."""
    snapshot = {"running": [
        _entry("a", "Watch",
               sent_to={"Reply with exactly the word ok and stop. [5ccbdc]":
                        {"count": 1, "last": "t"}}),
        _entry("b", "Hex", address="Reply with exactly the word ok and stop.",
               addressable=True),
    ]}
    edge = BobDaemon._build_mesh(snapshot)[0]
    assert edge["to_session"] == "b" and edge["to_nickname"] == "Hex"
    assert edge["resolved"] is True and edge["reachable"] is True
    # The address stays as it was typed: it is the thing you would type again,
    # and the panel shows the nickname anyway once it resolves.
    assert edge["to_address"].endswith("[5ccbdc]")


def test_a_name_that_merely_ends_in_brackets_is_not_mangled():
    """The strip is narrow on purpose — a hex ref in trailing brackets — because
    a name is free text and `deploy [staging]` is a name, not an address plus a
    disambiguator."""
    snapshot = {"running": [
        _entry("a", "Watch", sent_to={"deploy [staging]": {"count": 1}}),
        _entry("b", "Hex", address="deploy", addressable=True),
    ]}
    edge = BobDaemon._build_mesh(snapshot)[0]
    assert edge["resolved"] is False


def test_a_session_name_still_wins_over_a_subagent_id():
    snapshot = {"running": [
        _entry("a", "Audit", sent_to={"shared": {"count": 1}}),
        _entry("b", "Relay", address="shared", addressable=True),
        dict(_entry("c", "Mira"), subagent_rows=[{"agent_id": "shared"}]),
    ]}
    edge = BobDaemon._build_mesh(snapshot)[0]
    assert edge["to_session"] == "b" and edge["reachable"] is True


def test_overflow_marks_coverage_but_keeps_counting_known_recipient(tmp_path):
    lines = [_send(f"agent-{i}") for i in range(ss.MAX_MESH_EDGES + 1)]
    lines += [_send("agent-0", ts="invalid")]
    lines += [{"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": True, "content": "not delivered"}]}}]
    stats = ss.parse_transcript(_transcript(tmp_path, lines))
    assert stats.sent_to_partial is True
    assert len(stats.sent_to) == 32
    assert stats.sent_to["agent-0"] == {"count": 2, "last": "invalid"}
    assert ss.SessionStats().sent_to_partial is False


def test_typed_recipient_identity_is_not_trimmed(tmp_path):
    stats = ss.parse_transcript(_transcript(tmp_path, [_send(" target ")]))
    assert " target " in stats.sent_to
    assert "target" not in stats.sent_to
