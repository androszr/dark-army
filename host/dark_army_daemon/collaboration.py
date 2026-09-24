"""Bounded, read-only collaboration evidence. No I/O or action capability.

Exact addresses precede narrow hex-reference normalization. Within each lookup,
session addresses precede helper IDs; duplicates never select a first winner.
"""
from __future__ import annotations

import hashlib
import json
import re

MAX_NODES = 256
MAX_EDGES = 512
MAX_BYTES = 128 * 1024
MAX_ID_BYTES = 2048
BUCKETS = ("running", "sleeping", "waiting", "abandoned", "finished")
EXPLICIT_ENDS = frozenset({"ended", "closed", "stopped", "terminal closed", "no process"})


def unavailable(reason="projection_failed"):
    return dict(version=1, available=False, partial=True, reasons=[reason],
                nodes=[], edges=[], omitted_nodes=0, omitted_edges=0)


def _text(value):
    if not isinstance(value, str):
        return ""
    try:
        return value if len(value.encode("utf-8")) <= MAX_ID_BYTES else ""
    except UnicodeEncodeError:
        return ""


def _id(*parts):
    # Length-delimited JSON preserves typed identity even when addresses contain separators.
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode()).hexdigest()


def bare_address(value):
    return re.sub(r"\s*\[[0-9a-fA-F]{4,16}\]\s*$", "", value)


def build(snapshot, cards=()):
    """Project only allowlisted identity facts from final, enrolled agent rows."""
    result = dict(version=1, available=True, partial=False, reasons=[], nodes=[], edges=[],
                  omitted_nodes=0, omitted_edges=0)
    reasons = set()
    nodes, records, uncertain = {}, {}, set()
    session_addresses, helper_addresses = {}, {}
    edges = {}
    conflicting_observations = set()

    def add_node(node, facts):
        nid = node["id"]
        if nid in records and records[nid] != facts:
            uncertain.add(nid)
            reasons.add("contradictory_identity")
        records[nid] = facts
        nodes.setdefault(nid, node)
        return nid

    def address(index, key, nid):
        if key:
            index.setdefault(key, set()).add(nid)

    sources = []
    for category in BUCKETS:
        group = snapshot.get(category, [])
        if not isinstance(group, list):
            reasons.add("invalid_source")
            continue
        for row in group:
            if not isinstance(row, dict):
                reasons.add("invalid_source")
                continue
            sid = _text(row.get("session_id"))
            provider = _text(row.get("provider", "claude"))
            if not sid or not provider:
                result["omitted_nodes"] += 1
                reasons.add("invalid_identity")
                continue
            known_provider = provider in {"claude", "codex", "grok"}
            for key in ("address", "retained_address"):
                if row.get(key) and not _text(row[key]):
                    reasons.add("invalid_identity")
            retained = category == "finished" and row.get("alive") is not True
            nid = _id("session", provider, sid)
            addr = _text(row.get("retained_address") if retained else row.get("address"))
            node = dict(id=nid, kind="session", provider=provider, session_id=sid,
                        owner_session_id=sid, helper_id="", label=_text(row.get("nickname")) or sid,
                        address=addr, resolution="resolved" if known_provider else "unknown",
                        presence="retained" if retained else "present",
                        lifecycle=("ended" if row.get("end_reason") in EXPLICIT_ENDS else "unknown") if retained else ("unknown" if category == "abandoned" else "live"),
                        inbox_observed=None if retained else row.get("addressable") if isinstance(row.get("addressable"), bool) else None,
                        source="retained_registry" if retained and addr else "agents_snapshot",
                        reason=_text(row.get("end_reason")), cards=[])
            add_node(node, {k: v for k, v in node.items() if k != "cards"} | {"cwd": _text(row.get("cwd"))})
            address(session_addresses, addr, nid)
            sources.append((nid, row))
            if row.get("sent_to_partial"):
                reasons.add("recipient_cap")
            if provider != "claude" or "sent_to" not in row:
                reasons.add("message_evidence_unavailable")
            # Finished helper lists are not proof those helpers ended or are still live.
            if retained:
                continue
            helpers = row.get("subagent_rows") or []
            if not isinstance(helpers, list):
                reasons.add("invalid_source")
                continue
            parent_ids = {}
            for helper in helpers:
                if not isinstance(helper, dict):
                    reasons.add("invalid_source")
                    continue
                aid = _text(helper.get("agent_id"))
                if not aid:
                    result["omitted_nodes"] += 1
                    reasons.add("invalid_identity")
                    continue
                hid = _id("helper", provider, sid, aid)
                parent_ids[aid] = _text(helper.get("parent_agent_id"))
                hnode = dict(id=hid, kind="helper", provider=provider, session_id=sid,
                             owner_session_id=sid, helper_id=aid,
                             label=_text(helper.get("subagent_type")) or aid,
                             address=aid, resolution="resolved" if known_provider else "unknown",
                             presence="present", lifecycle="live", inbox_observed=False,
                             source="subagent_rows", reason="", cards=[])
                add_node(hnode, hnode | {"parent_agent_id": helper.get("parent_agent_id")})
                address(helper_addresses, aid, hid)
                if "parent_agent_id" not in helper:
                    reasons.add("ancestry_unavailable")
            for aid, parent in parent_ids.items():
                hid = _id("helper", provider, sid, aid)
                walk, seen = aid, set()
                while walk in parent_ids and walk not in seen:
                    seen.add(walk)
                    walk = parent_ids[walk]
                gap = "ancestry_cycle" if walk in seen else "ancestry_missing" if parent and parent not in parent_ids else ""
                if gap:
                    nodes[hid]["reason"] = gap
                    reasons.add(gap)
                    continue
                # A missing key is unknown ancestry, not an observed root relationship.
                raw_parent = records[hid].get("parent_agent_id")
                if not isinstance(raw_parent, str):
                    nodes[hid]["reason"] = "ancestry_unavailable"
                    continue
                if raw_parent and not _text(raw_parent):
                    nodes[hid]["reason"] = "ancestry_invalid"
                    reasons.add("ancestry_invalid")
                    result["omitted_edges"] += 1
                    continue
                parent_nid = _id("helper", provider, sid, parent) if parent else nid
                eid = _id("parent", parent_nid, hid)
                edges[eid] = dict(id=eid, kind="parent", source=parent_nid, target=hid,
                                  address="", count=0, last="")

    # The same session ID under multiple providers cannot prove a card's provider.
    identities = {}
    for nid, node in nodes.items():
        if node["kind"] == "session":
            identities.setdefault(node["session_id"], set()).add(nid)
    for card in cards:
        if not isinstance(card, dict):
            continue
        cid, root = _text(card.get("id")), _text(card.get("root"))
        if not cid or not root:
            continue
        for key, kind in (("session_id", "implementation"), ("refine_session_id", "refinement")):
            targets = identities.get(card.get(key), set())
            if len(targets) != 1:
                continue
            nid = next(iter(targets))
            if nid in uncertain:
                continue
            link = dict(id=cid, root=root, title=_text(card.get("title")), kind=kind)
            if link not in nodes[nid]["cards"]:
                nodes[nid]["cards"].append(link)
    for nid in uncertain:
        nodes[nid].update(resolution="unknown", presence="unknown", lifecycle="unknown",
                          inbox_observed=None, cards=[], reason="contradictory_identity",
                          label=nodes[nid]["session_id"], address="")
    for node in nodes.values():
        if node["kind"] == "helper" and node["id"] not in uncertain:
            owner = nodes.get(_id("session", node["provider"], node["owner_session_id"]))
            node["cards"] = list({(link["id"], link["root"]): link | {"kind": "inherited-from-parent"}
                                  for link in owner["cards"]}.values()) if owner else []
        node["cards"].sort(key=lambda c: (c["id"], c["kind"]))

    def resolve(typed):
        for lookup in dict.fromkeys((typed, bare_address(typed))):
            candidates = session_addresses.get(lookup) or helper_addresses.get(lookup) or set()
            if candidates:
                if len(candidates) == 1 and not candidates & uncertain:
                    return next(iter(candidates)), "resolved"
                return "", "ambiguous"
        return "", "unresolved"

    for source, row in sources:
        sent = row.get("sent_to") or {}
        if not isinstance(sent, dict):
            reasons.add("invalid_source")
            continue
        for typed, observed in sorted(sent.items(), key=lambda item: str(item[0])):
            if not _text(typed) or not isinstance(observed, dict):
                result["omitted_edges"] += 1
                reasons.add("invalid_identity")
                continue
            count = observed.get("count")
            if type(count) is not int or count < 0 or count > 2**53 - 1:
                result["omitted_edges"] += 1
                reasons.add("invalid_observation")
                continue
            target, resolution = resolve(typed)
            if not target:
                target = _id("recipient", source, typed)
                nodes[target] = dict(id=target, kind="recipient", provider="", session_id="",
                                     owner_session_id="", helper_id="", label=typed, address=typed,
                                     resolution=resolution, presence="unknown", lifecycle="unknown",
                                     inbox_observed=None, source="observed_address", reason="", cards=[])
            eid = _id("message", source, target, typed)
            observation = dict(id=eid, kind="message", source=source, target=target,
                               address=typed, count=count, last=_text(observed.get("last")))
            if eid in edges and edges[eid] != observation:
                conflicting_observations.add(eid)
                reasons.add("contradictory_observation")
            edges[eid] = observation
    # Contradictory identities cannot establish a specific relationship.
    invalid_edges = [eid for eid, edge in edges.items()
                     if edge["source"] in uncertain or edge["target"] in uncertain
                     or eid in conflicting_observations]
    for eid in invalid_edges:
        del edges[eid]
    result["omitted_edges"] += len(invalid_edges)
    # Stable order, independent of count or activity. Drop whole edges only.
    ordered_nodes = [nodes[k] for k in sorted(nodes)]
    result["omitted_nodes"] += max(0, len(ordered_nodes) - MAX_NODES)
    result["nodes"] = ordered_nodes[:MAX_NODES]
    included = {n["id"] for n in result["nodes"]}
    ordered_edges = sorted(edges.values(), key=lambda e: (e["kind"] != "parent", e["id"]))
    kept = [e for e in ordered_edges if e["source"] in included and e["target"] in included][:MAX_EDGES]
    result["omitted_edges"] += len(ordered_edges) - len(kept)
    result["edges"] = kept

    def stamp():
        if result["omitted_nodes"] or result["omitted_edges"]:
            reasons.add("projection_cap")
        result["reasons"] = sorted(reasons)
        result["partial"] = bool(reasons)
    stamp()
    while len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()) > MAX_BYTES:
        # Node payloads (including association lists) dominate the byte ceiling.
        removed = result["nodes"].pop()["id"]
        result["omitted_nodes"] += 1
        remaining = [e for e in result["edges"] if removed not in (e["source"], e["target"])]
        result["omitted_edges"] += len(result["edges"]) - len(remaining)
        result["edges"] = remaining
        stamp()
    return result


def legacy_mesh(projection):
    """Preserve the old wire shape without inventing ambiguous destinations."""
    nodes = {node["id"]: node for node in projection["nodes"]}
    result = []
    for edge in projection["edges"]:
        if edge["kind"] != "message":
            continue
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        resolved = target["resolution"] == "resolved"
        label = target["label"] if resolved else ""
        if target["kind"] == "helper":
            owner = nodes.get(_id("session", target["provider"], target["session_id"]))
            label = f"{label} · {owner['label']}" if owner else label
        result.append(dict(from_session=source["session_id"], from_nickname=source["label"],
                           to_address=edge["address"], to_session=target["session_id"] if resolved else "",
                           to_nickname=label, resolved=resolved,
                           reachable=bool(resolved and target["inbox_observed"] is True),
                           count=edge["count"], last=edge["last"]))
    return sorted(result, key=lambda e: (-e["count"], e["from_nickname"], e["to_address"]))
