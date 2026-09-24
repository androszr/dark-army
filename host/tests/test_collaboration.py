"""The collaboration success fixture exercises identity evidence, never delivery."""
import json
from copy import deepcopy

import pytest

from dark_army_daemon import collaboration as c


def session(sid, **facts):
    return dict(session_id=sid, provider="claude", nickname=sid, address=sid,
                addressable=False, sent_to={}, **facts)


def success_fixture():
    root = session("root")
    root["subagent_rows"] = [
        {"agent_id": "helper", "subagent_type": "Verifier", "parent_agent_id": ""},
        {"agent_id": "nested", "parent_agent_id": "helper"}]
    root["sent_to"] = {address: {"count": 2, "last": "not-a-time"} for address in
                       ("helper", "ended", "duplicate", "missing", "quiet")}
    ended = session("ended", retained_address="ended", end_reason="ended", alive=False)
    dup1, dup2 = session("dup1"), session("dup2")
    dup1["address"] = dup2["address"] = "duplicate"
    cards = [dict(id="card-a", root="/project", title="Task A", session_id="root"),
             dict(id="card-b", root="/project", title="Task B", refine_session_id="root")]
    return {"running": [root, dup1, dup2, session("quiet")], "finished": [ended]}, cards


def test_live_helper_ended_ambiguous_unresolved_and_present_without_inbox_distinct():
    snapshot, cards = success_fixture()
    evidence = c.build(snapshot, cards)
    by_address = {n["address"]: n for n in evidence["nodes"]}
    assert (by_address["helper"]["kind"], by_address["helper"]["lifecycle"]) == ("helper", "live")
    assert (by_address["ended"]["presence"], by_address["ended"]["lifecycle"]) == ("retained", "ended")
    assert by_address["duplicate"]["resolution"] == "ambiguous"
    assert by_address["missing"]["resolution"] == "unresolved"
    assert by_address["missing"]["lifecycle"] == "unknown"
    assert by_address["quiet"]["presence"] == "present"
    assert by_address["quiet"]["inbox_observed"] is False
    assert len({by_address[k]["id"] for k in ("helper", "ended", "duplicate", "missing")}) == 4
    assert len(by_address["helper"]["cards"]) == 2
    assert {link["kind"] for link in by_address["helper"]["cards"]} == {"inherited-from-parent"}
    assert {edge["count"] for edge in evidence["edges"] if edge["kind"] == "message"} == {2}
    assert "delivery" not in json.dumps(evidence)
    assert "causation" not in json.dumps(evidence)


def test_exact_before_normalized_and_namespace_precedence():
    root = session("root")
    root["sent_to"] = {"target [abcd]": {"count": 1}, "helper": {"count": 1}}
    root["subagent_rows"] = [{"agent_id": "target [abcd]", "parent_agent_id": ""},
                            {"agent_id": "helper", "parent_agent_id": ""}]
    evidence = c.build({"running": [root, session("target"), session("helper")]})
    nodes = {n["id"]: n for n in evidence["nodes"]}
    messages = {e["address"]: nodes[e["target"]] for e in evidence["edges"] if e["kind"] == "message"}
    assert messages["target [abcd]"]["kind"] == "helper"
    assert messages["helper"]["kind"] == "session"


@pytest.mark.parametrize("address", ["literal [work]", "literal [ab]", "literal [0123456789abcdef0]"])
def test_only_narrow_hex_reference_is_normalized(address):
    assert c.bare_address(address) == address


def test_duplicate_helpers_are_owner_qualified_and_address_reuse_is_ambiguous():
    a, b = session("a"), session("b")
    for row in (a, b):
        row["subagent_rows"] = [{"agent_id": "helper", "parent_agent_id": ""}]
    a["sent_to"] = {"helper": {"count": 1}, "reused": {"count": 1}}
    b["address"] = "reused"
    ended = session("old", retained_address="reused", end_reason="closed")
    evidence = c.build({"running": [a, b], "finished": [ended]})
    assert len([n for n in evidence["nodes"] if n["kind"] == "helper"]) == 2
    recipients = [n for n in evidence["nodes"] if n["kind"] == "recipient"]
    assert len(recipients) == 2 and all(n["resolution"] == "ambiguous" for n in recipients)
    assert all(not e["resolved"] and not e["to_session"] for e in c.legacy_mesh(evidence))


def test_duplicate_identity_conflicts_remove_navigation_and_card_associations():
    a = session("same", cwd="/one")
    b = session("same", cwd="/two")
    evidence = c.build({"running": [a, b]}, [dict(id="a", root="/one", session_id="same")])
    assert len(evidence["nodes"]) == 1
    node = evidence["nodes"][0]
    assert node["resolution"] == "unknown" and not node["cards"]
    assert "contradictory_identity" in evidence["reasons"]


def test_duplicate_agreeing_rows_deduplicate_and_result_is_deterministic():
    snapshot, cards = success_fixture()
    snapshot["running"].append(deepcopy(snapshot["running"][0]))
    first = c.build(snapshot, cards)
    snapshot["running"].reverse()
    assert c.build(snapshot, list(reversed(cards))) == first


@pytest.mark.parametrize("other_observation", [
    {"count": 9, "last": "first"}, {"count": 1, "last": "second"},
    {"count": 9, "last": "second"},
])
def test_conflicting_duplicate_message_observations_are_omitted_in_both_orders(other_observation):
    first = session("root")
    first["sent_to"] = {"missing": {"count": 1, "last": "first"}}
    second = deepcopy(first)
    second["sent_to"]["missing"] = other_observation
    evidence = c.build({"running": [first, second]})
    assert c.build({"running": [second, first]}) == evidence
    assert evidence["partial"]
    assert "contradictory_observation" in evidence["reasons"]
    assert evidence["omitted_edges"] == 1
    assert evidence["edges"] == []


@pytest.mark.parametrize("parent", ["p" * 2049, "\ud800"])
def test_invalid_explicit_parent_is_an_omitted_ancestry_gap_not_a_root(parent):
    row = session("root")
    row["subagent_rows"] = [{"agent_id": "helper", "parent_agent_id": parent}]
    evidence = c.build({"running": [row]})
    assert evidence["edges"] == []
    assert evidence["partial"]
    assert evidence["omitted_edges"] == 1
    assert "ancestry_invalid" in evidence["reasons"]
    assert next(n for n in evidence["nodes"] if n["kind"] == "helper")["reason"] == "ancestry_invalid"


@pytest.mark.parametrize("reason", ["evicted", "", "lost contact", "quiet"])
def test_retained_silence_is_not_ended(reason):
    evidence = c.build({"finished": [session("quiet", retained_address="quiet", end_reason=reason)]})
    assert evidence["nodes"][0]["lifecycle"] == "unknown"


def test_live_idle_finished_bucket_stays_present():
    evidence = c.build({"finished": [session("quiet", alive=True)]})
    assert evidence["nodes"][0]["presence"] == "present"
    assert evidence["nodes"][0]["lifecycle"] == "live"


def test_ancestry_gaps_cycles_and_roles_never_manufacture_parent_links():
    root = session("root", crew_trail={"bc-verifier": "someone"})
    root["subagent_rows"] = [{"agent_id": "missing", "parent_agent_id": "absent"},
                            {"agent_id": "self", "parent_agent_id": "self"},
                            {"agent_id": "a", "parent_agent_id": "b"},
                            {"agent_id": "b", "parent_agent_id": "a"},
                            {"agent_id": "unknown"}]
    evidence = c.build({"running": [root]})
    assert evidence["edges"] == []
    assert len(evidence["nodes"]) == 6
    assert {"ancestry_missing", "ancestry_cycle", "ancestry_unavailable"} <= set(evidence["reasons"])


def test_no_card_inference_from_names_or_stages_or_cross_provider_session_collision():
    a = session("root")
    other = session("elsewhere")
    other["nickname"] = "root"
    cards = [dict(id="a", root="/project", title="root", workflow="root", session_id="absent")]
    assert all(not n["cards"] for n in c.build({"running": [a, other]}, cards)["nodes"])
    duplicate = dict(a, provider="codex")
    cards[0]["session_id"] = "root"
    assert all(not n["cards"] for n in c.build({"running": [a, duplicate]}, cards)["nodes"])


def test_source_coverage_invalid_identity_and_privacy():
    root = session("root", sent_to_partial=True, token="private", inbox_path="/private",
                   port=123, claim="secret", prompt="never publish", crew_trail={"fake": "person"})
    root["sent_to"] = {"x" * (c.MAX_ID_BYTES + 1): {"count": 1}, "safe": {"count": 1, "message": "body"}}
    evidence = c.build({"running": [root, dict(session("codex"), provider="codex")]})
    assert {"recipient_cap", "invalid_identity", "message_evidence_unavailable"} <= set(evidence["reasons"])
    blob = json.dumps(evidence)
    for forbidden in ("private", "secret", "body", "never publish", "crew_trail", '"port"', '"token"', '"claim"'):
        assert forbidden not in blob
    assert evidence["omitted_edges"] == 1 and evidence["partial"]


def test_caps_keep_whole_edges_and_explanatory_notice():
    rows = [session(str(i)) for i in range(300)]
    for row in rows:
        row["sent_to"] = {str(i): {"count": 1} for i in range(32)}
    evidence = c.build({"running": rows})
    assert len(evidence["nodes"]) <= c.MAX_NODES
    assert len(evidence["edges"]) <= c.MAX_EDGES
    assert evidence["omitted_nodes"] > 0 and evidence["omitted_edges"] > 0
    ids = {n["id"] for n in evidence["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in evidence["edges"])
    assert len(json.dumps(evidence, separators=(",", ":")).encode()) <= c.MAX_BYTES
    assert evidence["partial"] and "projection_cap" in evidence["reasons"]


def test_utf8_byte_cap_and_card_payloads_are_bounded():
    rows = [session(str(i), cwd="/project") for i in range(256)]
    cards = [dict(id=str(i), root="/project", title="ą" * 400, session_id=str(i % 3)) for i in range(500)]
    for row in rows:
        row["nickname"] = "ł" * 1000
    evidence = c.build({"running": rows}, cards)
    assert len(json.dumps(evidence, separators=(",", ":")).encode()) <= c.MAX_BYTES
    assert evidence["partial"]


def test_unknown_provider_cannot_grant_navigation():
    evidence = c.build({"running": [dict(session("future"), provider="future")]})
    assert evidence["nodes"][0]["resolution"] == "unknown"
    assert evidence["partial"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_final_provider_picture_publishes_before_observer_and_failure_isolated(monkeypatch, fail):
    import threading
    from dark_army_daemon.daemon import BobDaemon
    observed = []
    class Observer:
        def on_agents_change(self, snapshot):
            observed.append((deepcopy(snapshot), d._collaboration))
    d = BobDaemon(observer=Observer())
    root = session("root")
    d._collect_agent_stubs = lambda: []
    d._enrich_agent_stubs = lambda _: {"running": [root]}
    def final_helpers(snapshot):
        snapshot["running"][0]["subagent_rows"] = [{"agent_id": "final", "parent_agent_id": ""}]
    d._apply_grok_live_subagents = final_helpers
    d._apply_grok_finished_demote = lambda _: None
    d._apply_terminal_titles = lambda *_: None
    reconciled = []
    d._reconcile_board = lambda snapshot: reconciled.append(snapshot) or False
    d._deliver_alerts = lambda: None
    d._drain_pending_agents_push = lambda: None
    d._board_state = {"cards": [dict(id="card", root="/project", session_id="root")]}
    original = c.build
    main_thread = threading.get_ident()
    calls = []
    def projection(snapshot, cards):
        calls.append(threading.get_ident())
        assert cards is not d._board_state["cards"]
        if fail:
            raise ValueError("fixture failure")
        return original(snapshot, cards)
    monkeypatch.setattr(c, "build", projection)
    await d._push_agents_snapshot()
    assert len(calls) == 1 and calls[0] != main_thread
    assert len(observed) == len(reconciled) == 1
    assert observed[0][0]["running"][0]["subagent_rows"][0]["agent_id"] == "final"
    if fail:
        assert not observed[0][1]["available"]
    else:
        assert any(n["helper_id"] == "final" for n in observed[0][1]["nodes"])
