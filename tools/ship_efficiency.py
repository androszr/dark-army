#!/usr/bin/env python3
"""Offline accounting for what the ship workflow makes its agents read.

Stdlib only. It **never launches an agent and never executes a recorded
command**: every figure here is a byte count, a digest or a field copied out
of a run record somebody else wrote. Six subcommands:

``inventory --root . [--output FILE] [--check] [--baseline FILE] [--source TEXT]``
    Measure the instruction documents on disk, the explicit load set of every
    role under ``docs/agent-context.json`` (or the legacy rule when the map is
    absent) and three mandatory scenarios. ``--check`` proves the compact
    root's byte ceiling, the aggregate load reduction against the recorded
    baseline, the preservation map in ``docs/ship-efficiency.md`` and the
    conservative fallback. ``--source`` states in words where the measured
    tree came from and is written into the output as ``source`` — the
    recorded baseline fixture carries one, like the paragraph fixture.

``report --baseline RUNS --candidate RUNS --output FILE``
    Import run evidence (``host/tests/fixtures/ship_efficiency/*.json`` is the
    shape) and aggregate per-role and end-to-end input/output tokens, cache
    fields where supplied, read volume, duplicate loads, command executions,
    repairs and elapsed time. Every unique provider/session/turn counts once;
    a parent total known to include its children is never added to them;
    unavailable fields stay unavailable, never zero.

``validate-evaluation DOC --fixtures DIR``
    The bounded paired evaluation the document describes must be complete
    and evidenced: it exits non-zero otherwise. Every one of the six
    scenarios needs a source-linked, redacted record for each arm in ``DIR``
    (``scenario-N-baseline.json`` / ``scenario-N-candidate.json``) whose
    candidate caught every seeded fault and every finding the baseline
    caught, preserved clean verdicts, ran the required gates, kept models and
    reasoning unchanged, shared no pass across roles and leaked no verifier
    report. Vacuous evidence is refused: a record that calls itself
    synthetic, an empty gate list, unstated models or reasoning, a verdict
    off the closed set, or a seeded-fault scenario with no seeded fault or a
    clean verdict. A scenario declared ``not-run``, or one whose records are
    missing, is named in the output as a missing run and fails the check —
    the document must then say so in words, claim nothing from it and hold
    the reduced default ``pending``. There is no way to pass this check
    without the twelve records.

``baseline-paragraphs FILE --output FIXTURE --source TEXT``
    Record the paragraph digests of an old root document (the shape
    ``check_preservation`` reads) with the provenance stated in ``--source``.
    The ship baseline is the saved pre-ship copy of ``CLAUDE.md`` — the file
    as the working tree held it when the run began — never ``HEAD`` plus a
    patch reapplied by hand.

``validate-report DOC``
    Static counts, observed usage fields and monetary amounts are distinct
    sections; coverage and missing usage are explicit; a claimed runtime input
    reduction is computed only from complete paired observations, and an
    unmet or unmeasured target is stated as such with no cost-saving claim.
    The ``## Delegation`` section and its ``shunt`` claim (``measured`` /
    ``unmeasured``) are required too.

``shunt --ledgers DIR [--output FILE]``
    Count what the shunt skill's delegation ledgers recorded
    (``~/.dark-army/shunt/<session id>.jsonl``, one JSON object per
    line): every ``*.jsonl`` under ``DIR``, de-duplicated by
    ``delegation_id``, reported as ``delegations``, ``lines_kept_out`` and
    ``worker_cost`` — ``unavailable`` unless every record carries a measured
    USD figure, never zero — in total, per provider and per mode. It
    launches nothing and reads only what the wrappers wrote; a ledger says
    what was delegated, not what it saved, and this tool claims nothing
    about the main model's context.

The document declares what it claims in marker comments the validators read
(``<!-- ship-efficiency: ... -->``), so a sentence cannot drift away from the
figure behind it unnoticed.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import statistics
import sys
from pathlib import Path

SCHEMA = 1
CONTEXT_MAP = "docs/agent-context.json"
REPORT_DOC = "docs/ship-efficiency.md"
FIXTURES = "host/tests/fixtures/ship_efficiency"
BASELINE_INVENTORY = f"{FIXTURES}/baseline-inventory.json"
BASELINE_PARAGRAPHS = f"{FIXTURES}/claude-md-baseline.json"
ROOT_DOC = "CLAUDE.md"
ROOT_CEILING = 30_000
LOAD_REDUCTION = 0.28
RUNTIME_TARGET = 0.15
DIGEST_CHARS = 12

CORE = ("CLAUDE.md", "AGENTS.md")
ROLES = (
    "bc-planner", "bc-implementer", "bc-verifier", "bc-bug-auditor",
    "bc-integration-reviewer", "bc-security-reviewer",
)
PLAN_ROLES = ("bc-planner",)
IMPLEMENT_ROLES = ("bc-implementer", "bc-verifier", "bc-bug-auditor")
REVIEW_ROLES = ("bc-integration-reviewer", "bc-security-reviewer")
PROVIDERS = {"claude": ".claude/skills/ship/SKILL.md",
             "codex": ".agents/skills/ship/SKILL.md"}
#: Ship's scout mode is an alias for the scout skill, so the adapter names
#: that skill's own adapter where the other modes name a reference.
SCOUT_SKILL = ".claude/skills/scout/SKILL.md"
REFERENCE_RE = re.compile(
    r"`((?:\.claude/skills/ship/)?references/(?:common|plan|implement)\.md"
    r"|\.claude/skills/scout/SKILL\.md)`")

#: The three mandatory scenarios the budget is measured on. ``paths`` are the
#: kind of delta each one produces; the map turns them into references.
SCENARIOS = {
    "plan-only": {
        "mode": "plan",
        "roles": PLAN_ROLES,
        "surfaces": ["tools"],
        "paths": ["tools/ship_efficiency.py", "docs/ship-efficiency.md"],
    },
    "python-only": {
        "mode": "implement",
        "roles": IMPLEMENT_ROLES,
        "surfaces": ["host"],
        "paths": ["host/dark_army_daemon/session_stats.py",
                  "host/tests/test_session_stats.py"],
    },
    "cross-surface": {
        "mode": "implement",
        "roles": IMPLEMENT_ROLES + REVIEW_ROLES,
        "surfaces": ["host", "panel"],
        "paths": ["host/dark_army_daemon/api_server.py",
                  "panel/Sources/BobPanel/DaemonClient.swift",
                  "host/tests/test_api_server.py"],
    },
}

MARKER_RE = re.compile(r"<!--\s*ship-efficiency:\s*(.*?)\s*-->", re.S)


class ShipEfficiencyError(Exception):
    """A document or record is not in the shape this tool accounts for."""


# --- paragraphs and digests --------------------------------------------------

_LIST_ITEM = re.compile(r"^(\s*)(?:[-*]|\d+\.) ")


def paragraphs(text: str) -> list[str]:
    """Blank-line separated blocks; a fenced code block is one block, a list
    item at the indentation of the block's first line starts a new block,
    and headings are not blocks (a heading is a label, not a rule)."""
    out: list[str] = []
    current: list[str] = []
    fenced = False
    indent = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            current.append(line)
            if not fenced:
                out.append("\n".join(current))
                current = []
            continue
        if fenced:
            current.append(line)
            continue
        if not stripped:
            if current:
                out.append("\n".join(current))
                current = []
            continue
        item = _LIST_ITEM.match(line)
        if item and current and len(item.group(1)) <= indent:
            out.append("\n".join(current))
            current = []
        if not current:
            indent = len(line) - len(line.lstrip())
        current.append(line)
    if current:
        out.append("\n".join(current))
    return [block for block in out
            if not re.match(r"^\s*#{1,6} ", block) and block.strip()]


def normalise(block: str) -> str:
    return " ".join(block.split())


def digest(block: str) -> str:
    return hashlib.sha256(normalise(block).encode("utf-8")).hexdigest()[:DIGEST_CHARS]


def paragraph_digests(text: str) -> dict[str, str]:
    """digest → block, first occurrence wins."""
    out: dict[str, str] = {}
    for block in paragraphs(text):
        out.setdefault(digest(block), block)
    return out


def file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


HEAD_CHARS = 70
#: A head-matched block shorter than this fraction of the recorded paragraph
#: is refused as gutted rather than noted as edited.
GUTTED_FRACTION = 0.5


def baseline_paragraphs(text: str, source: str) -> dict:
    """The fixture ``check_preservation`` reads: one entry per distinct
    paragraph digest, in document order, each with the nearest ``#`` line
    above it (fenced or not — a label, only for a human reading the map), the
    first ``HEAD_CHARS`` characters and its length in words — the figure
    ``preservation_report`` holds a head-matched block against."""
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    section = ""
    pos = 0
    for block in paragraphs(text):
        start = text.index(block, pos)
        for line in text[pos:start].split("\n"):
            if re.match(r"^#{1,6} ", line):
                section = line.strip()
        pos = start
        d = digest(block)
        if d in seen:
            continue
        seen.add(d)
        flat = normalise(block)
        entries.append({"digest": d, "section": section,
                        "head": flat[:HEAD_CHARS], "words": len(flat.split())})
    return {"source": source, "bytes": len(text.encode("utf-8")),
            "paragraphs": entries}


# --- the context map ---------------------------------------------------------

def load_context_map(root: Path) -> dict | None:
    path = root / CONTEXT_MAP
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ShipEfficiencyError(f"{CONTEXT_MAP}: unknown version")
    return data


def _matches(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern):
            return True
        if pattern.endswith("/") and path.startswith(pattern):
            return True
    return False


def select_references(context: dict, surfaces, paths) -> tuple[set[str], list[str]]:
    """The reference names a scope selects, and why.

    The union of every surface's references, every path's references and the
    cross-cutting additions; **any** unknown surface, unmapped path or empty
    scope selects the conservative fallback — every reference — and the
    second element says which input widened it.
    """
    refs = context["references"]
    everything = set(refs)
    chosen: set[str] = set()
    widened: list[str] = []
    surfaces = [s for s in (surfaces or []) if s]
    paths = [p for p in (paths or []) if p]
    if not surfaces and not paths:
        return everything, ["empty scope"]
    surface_map = context.get("surfaces", {})
    for surface in surfaces:
        names = surface_map.get(surface)
        if names is None:
            widened.append(f"unknown surface {surface!r}")
            continue
        chosen.update(names)
    for path in paths:
        hit = False
        for name, ref in refs.items():
            if _matches(path, ref.get("paths", [])):
                chosen.add(name)
                hit = True
        if not hit:
            widened.append(f"unmapped path {path!r}")
    for rule in context.get("cross_cutting", []):
        if any(_matches(p, rule.get("paths", [])) for p in paths) or \
                set(rule.get("surfaces", [])) & set(surfaces):
            chosen.update(rule.get("add", []))
    if widened:
        return everything, widened
    unknown = chosen - everything
    if unknown:
        raise ShipEfficiencyError(f"map names references that do not exist: {sorted(unknown)}")
    return chosen, widened


def reference_paths(context: dict, names) -> list[str]:
    out: list[str] = []
    for name in sorted(names):
        ref = context["references"][name]
        out.append(ref["path"])
        out.extend(ref.get("with", []))
    return sorted(set(out))


# --- workflows ---------------------------------------------------------------

def referenced_workflow_files(adapter_text: str) -> list[str]:
    """The reference documents an adapter names, in first-mention order."""
    seen: list[str] = []
    for match in REFERENCE_RE.finditer(adapter_text):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


def _reference_location(adapter: str, reference: str) -> str:
    """Repo-relative path of a reference an adapter at ``adapter`` names."""
    if reference.startswith(".claude/"):
        return reference
    return str(Path(adapter).parent / reference).replace("\\", "/")


def resolve_workflow(root: Path, adapter: str, mode: str | None = None) -> str:
    """The adapter's text followed by every reference it names (or the mode's
    two), each read from disk — what an assistant has in front of it after
    doing what the adapter says."""
    text = (root / adapter).read_text(encoding="utf-8")
    parts = [text]
    for reference in workflow_reference_set(adapter, text, mode):
        parts.append((root / reference).read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def workflow_reference_set(adapter: str, text: str, mode: str | None) -> list[str]:
    """Repo-relative reference paths the adapter loads for ``mode`` (both
    modes when ``None``), each once, in the adapter's own order."""
    if mode is None:
        wanted = ("common", "plan", "implement", "scout")
    elif mode == "scout":
        wanted = ("scout",)  # the alias loads the scout skill alone
    else:
        wanted = ("common", mode)
    out: list[str] = []
    for reference in referenced_workflow_files(text):
        stem = "scout" if reference == SCOUT_SKILL else Path(reference).stem
        location = _reference_location(adapter, reference)
        if stem in wanted and location not in out:
            out.append(location)
    return out


def resolve_rendered_workflow(mapping: dict, adapter: str, mode: str | None = None) -> str:
    """``resolve_workflow`` over a rendered pack mapping (``pack_render.render``)."""
    text = mapping[adapter].decode("utf-8")
    parts = [text]
    for reference in workflow_reference_set(adapter, text, mode):
        if reference not in mapping:
            raise ShipEfficiencyError(f"{adapter} names {reference}, which the pack did not render")
        parts.append(mapping[reference].decode("utf-8"))
    return "\n\n".join(parts)


# --- inventory ---------------------------------------------------------------

def _doc(root: Path, rel: str) -> dict:
    path = root / rel
    if not path.is_file():
        raise ShipEfficiencyError(f"missing instruction document: {rel}")
    data = path.read_bytes()
    return {"bytes": len(data), "sha256": file_digest(data)}


def _role_settings(root: Path, role: str) -> dict:
    """Sandbox, reasoning and model as the shim states them (Codex) and the
    brief's tool line (Claude); read as text so the tool stays stdlib-only
    on 3.9 too."""
    shim = root / f".codex/agents/{role}.toml"
    out = {"brief": f".claude/agents/{role}.md", "shim": f".codex/agents/{role}.toml"}
    if shim.is_file():
        text = shim.read_text(encoding="utf-8")
        for key in ("sandbox_mode", "model_reasoning_effort", "model"):
            match = re.search(rf'^{key}\s*=\s*"([^"]*)"', text, re.M)
            out[key] = match.group(1) if match else None
    brief = root / out["brief"]
    if brief.is_file():
        match = re.search(r"^tools:\s*(.*)$", brief.read_text(encoding="utf-8"), re.M)
        out["tools"] = match.group(1).strip() if match else ""
    return out


def legacy_loads(root: Path) -> dict:
    """The load rule before the map existed: every role reads the two roots
    and its brief; the parent reads the roots and the whole skill."""
    roles = {}
    for role in ROLES:
        roles[role] = list(CORE) + [f".claude/agents/{role}.md"]
    workflow = {}
    for provider, adapter in PROVIDERS.items():
        workflow[provider] = {"plan": list(CORE) + [adapter],
                             "implement": list(CORE) + [adapter]}
    return {"rule": "legacy", "roles": roles, "workflow": workflow}


def mapped_loads(root: Path, context: dict, scenario: dict) -> dict:
    """The load rule under the map, for one scenario's scope."""
    names, widened = select_references(context, scenario["surfaces"], scenario["paths"])
    refs = reference_paths(context, names)
    core = list(context.get("core", CORE))
    roles = {}
    for role in ROLES:
        roles[role] = core + [f".claude/agents/{role}.md"] + refs
    workflow = {}
    for provider, adapter in PROVIDERS.items():
        text = (root / adapter).read_text(encoding="utf-8")
        workflow[provider] = {
            mode: core + [adapter] + workflow_reference_set(adapter, text, mode)
            for mode in ("plan", "implement")
        }
    return {"rule": "mapped", "references": sorted(names), "widened": widened,
            "roles": roles, "workflow": workflow}


def _scenario_bytes(root: Path, loads: dict, scenario: dict, provider: str) -> dict:
    parent = loads["workflow"][provider][scenario["mode"]]
    per_role = {"parent": parent}
    for role in scenario["roles"]:
        per_role[role] = loads["roles"][role]
    sizes = {}
    total = 0
    for who, paths in per_role.items():
        n = sum(_doc(root, p)["bytes"] for p in dict.fromkeys(paths))
        sizes[who] = {"paths": list(dict.fromkeys(paths)), "bytes": n}
        total += n
    return {"roles": sizes, "bytes": total}


def inventory(root: Path) -> dict:
    root = Path(root)
    context = load_context_map(root)
    documents: dict[str, dict] = {}
    scenarios = {}
    for name, scenario in SCENARIOS.items():
        loads = legacy_loads(root) if context is None else mapped_loads(root, context, scenario)
        measured = _scenario_bytes(root, loads, scenario, "claude")
        for who in measured["roles"].values():
            for p in who["paths"]:
                documents.setdefault(p, _doc(root, p))
        scenarios[name] = {
            "mode": scenario["mode"], "surfaces": scenario["surfaces"],
            "paths": scenario["paths"], "rule": loads["rule"],
            "references": loads.get("references", []),
            "widened": loads.get("widened", []),
            **measured,
        }
    for rel in (ROOT_DOC, "AGENTS.md"):
        if (root / rel).is_file():
            documents.setdefault(rel, _doc(root, rel))
    roles = {role: _role_settings(root, role) for role in ROLES}
    aggregate = sum(s["bytes"] for s in scenarios.values())
    return {
        "schema": SCHEMA,
        "rule": "legacy" if context is None else "mapped",
        "documents": documents,
        "roles": roles,
        "scenarios": scenarios,
        "aggregate_bytes": aggregate,
        "note": ("Byte counts of explicit load sets; not observed request tokens, "
                 "not provider billing. A path is counted once per load set."),
    }


def inside_checkout(root: Path, rel: str) -> bool:
    """Whether ``rel`` names a file **inside** ``root`` once both are fully
    resolved: ``..`` segments and symlinks are followed before the test, so
    ``docs/../../x.md`` and a link out of the tree are both refused, and an
    absolute path is only admitted when it lands under the root."""
    if not rel:
        return False
    try:
        top = Path(root).resolve()
        target = (top / rel).resolve()
    except (OSError, RuntimeError):
        return False
    return top in target.parents


# --- the preservation map ----------------------------------------------------

def preservation_map(doc_text: str) -> dict:
    """The fenced JSON block under ``## Preservation map``."""
    match = re.search(r"^## Preservation map\n(.*?)(?=^## |\Z)", doc_text, re.M | re.S)
    if not match:
        raise ShipEfficiencyError("the report has no '## Preservation map' section")
    fence = re.search(r"```json\n(.*?)\n```", match.group(1), re.S)
    if not fence:
        raise ShipEfficiencyError("the preservation map has no fenced json block")
    data = json.loads(fence.group(1))
    for key in ("retained", "relocated", "exceptions"):
        if key not in data:
            raise ShipEfficiencyError(f"preservation map lacks {key!r}")
    return data


def check_preservation(root: Path, pmap: dict, baseline: dict) -> list[str]:
    """The problems alone; ``preservation_report`` has the edited list too."""
    return preservation_report(root, pmap, baseline)[0]


def preservation_report(root: Path, pmap: dict, baseline: dict) -> tuple[list[str], list[str]]:
    """``(problems, edited)``. A relocated paragraph is found in its
    destination by digest — verbatim — or, failing that, by its **opening**:
    the first ``HEAD_CHARS`` characters the baseline fixture recorded. The
    second match means the paragraph travelled whole and a later change
    edited it in its new home (a subject document is a living contract); it
    is listed under ``edited``, never counted lost. A paragraph with neither
    is a problem. The opening is an anchor, not a proof of content, so the
    anchor is held against the length the fixture recorded (``words``): a
    head-matched block under ``GUTTED_FRACTION`` of it is a **problem** — a
    paragraph gutted or reversed after its opening is not an edit in place —
    and every edited note states ``old→new words`` so a reader can judge the
    rest. What a bounded edit did to the paragraph is that edit's own review."""
    problems: list[str] = []
    edited: list[str] = []
    root_digests = paragraph_digests((root / ROOT_DOC).read_text(encoding="utf-8"))
    heads = {e.get("digest"): e.get("head", "") for e in baseline.get("paragraphs", [])}
    words = {e.get("digest"): e.get("words") for e in baseline.get("paragraphs", [])}
    destinations: dict[str, dict[str, str]] = {}

    def digests_of(rel: str) -> dict[str, str]:
        if rel not in destinations:
            path = root / rel
            if not path.is_file():
                problems.append(f"preservation destination missing: {rel}")
                destinations[rel] = {}
            else:
                destinations[rel] = paragraph_digests(path.read_text(encoding="utf-8"))
        return destinations[rel]

    retained = set(pmap["retained"])
    relocated = dict(pmap["relocated"])
    exceptions = dict(pmap["exceptions"])
    overlap = (retained & set(relocated)) | (retained & set(exceptions)) | (set(relocated) & set(exceptions))
    if overlap:
        problems.append(f"digests listed twice in the preservation map: {sorted(overlap)[:5]}")
    for d in sorted(retained):
        if d in root_digests:
            continue
        # A retained paragraph is a living rule too: one edited in place is
        # found by its recorded opening, held to the same gutted floor, and
        # noted -- never counted lost, and never a red baseline for every run
        # after the edit.
        head = heads.get(d, "")
        matched = [normalise(block) for block in root_digests.values()
                   if head and normalise(block)[:HEAD_CHARS] == head]
        if matched:
            found = max(len(block.split()) for block in matched)
            recorded = words.get(d)
            if isinstance(recorded, int) and recorded > 0 and found < recorded * GUTTED_FRACTION:
                problems.append(
                    f"retained paragraph {d} ({head[:40]!r}) in {ROOT_DOC} keeps its opening but "
                    f"{recorded}→{found} words: gutted, not edited in place")
                continue
            sizes = f"{recorded}→{found} words" if isinstance(recorded, int) else f"{found} words now"
            edited.append(f"retained paragraph {d} ({head[:40]!r}) was edited in {ROOT_DOC} "
                          f"in place ({sizes})")
            continue
        problems.append(f"retained paragraph {d} is not in {ROOT_DOC}")
    for d, dest in sorted(relocated.items()):
        if not inside_checkout(root, dest):
            problems.append(f"relocated {d} escapes the checkout: {dest}")
            continue
        blocks = digests_of(dest)
        if d in blocks:
            continue
        head = heads.get(d, "")
        matched = [normalise(block) for block in blocks.values()
                   if head and normalise(block)[:HEAD_CHARS] == head]
        if matched:
            found = max(len(block.split()) for block in matched)
            recorded = words.get(d)
            if isinstance(recorded, int) and recorded > 0 and found < recorded * GUTTED_FRACTION:
                problems.append(
                    f"relocated paragraph {d} ({head[:40]!r}) in {dest} keeps its opening but "
                    f"{recorded}→{found} words: gutted, not edited in place")
                continue
            sizes = f"{recorded}→{found} words" if isinstance(recorded, int) else f"{found} words now"
            edited.append(f"relocated paragraph {d} ({head[:40]!r}) was edited in {dest} "
                          f"after the move ({sizes})")
            continue
        problems.append(f"relocated paragraph {d} is not in {dest}")
    for d, why in sorted(exceptions.items()):
        if not isinstance(why, dict) or not why.get("why") or not why.get("replacement"):
            problems.append(f"exception {d} needs 'why' and 'replacement'")
    accounted = retained | set(relocated) | set(exceptions)
    for entry in baseline.get("paragraphs", []):
        d = entry["digest"]
        if d not in accounted:
            problems.append(f"baseline paragraph {d} ({entry.get('head', '')[:40]!r}) has no mapped destination")
    for d in sorted(accounted):
        if d not in {e["digest"] for e in baseline.get("paragraphs", [])}:
            problems.append(f"map lists {d}, which the baseline never had")
    return problems, edited


def check_inventory(root: Path, inv: dict, baseline_inv: dict | None,
                    edited_out: list[str] | None = None) -> list[str]:
    """Problems with the tree; ``edited_out``, when given, receives the
    relocated paragraphs a later change edited in place (notes, not
    problems)."""
    problems: list[str] = []
    size = inv["documents"].get(ROOT_DOC, {}).get("bytes", 0)
    if size > ROOT_CEILING:
        problems.append(f"{ROOT_DOC} is {size:,} bytes, over the {ROOT_CEILING:,}-byte compact ceiling")
    context = load_context_map(root)
    if context is None:
        problems.append(f"{CONTEXT_MAP} is missing; every role is on the legacy rule")
        return problems
    for name, ref in context["references"].items():
        rel = ref.get("path", "")
        if not inside_checkout(root, rel):
            problems.append(f"reference {name} escapes the checkout: {rel}")
        elif not (root / rel).is_file():
            problems.append(f"reference {name} names a missing document: {rel}")
        for extra in ref.get("with", []):
            if not inside_checkout(root, extra):
                problems.append(f"reference {name} escapes the checkout: {extra}")
            elif not (root / extra).is_file():
                problems.append(f"reference {name} points at a missing contract: {extra}")
    for surface, names in context.get("surfaces", {}).items():
        for n in names:
            if n not in context["references"]:
                problems.append(f"surface {surface!r} selects unknown reference {n!r}")
    for rule in context.get("cross_cutting", []):
        for n in rule.get("add", []):
            if n not in context["references"]:
                problems.append(f"cross-cutting rule adds unknown reference {n!r}")
    everything = set(context["references"])
    for probe in ({"surfaces": ["no-such-surface"], "paths": []},
                  {"surfaces": [], "paths": ["somewhere/new/file.rs"]},
                  {"surfaces": [], "paths": []}):
        chosen, widened = select_references(context, probe["surfaces"], probe["paths"])
        if chosen != everything or not widened:
            problems.append(f"unknown scope {probe} did not select the conservative fallback")
    for provider, adapter in PROVIDERS.items():
        text = (root / adapter).read_text(encoding="utf-8")
        for mode in ("plan", "implement"):
            refs = workflow_reference_set(adapter, text, mode)
            stems = {Path(r).stem for r in refs}
            if stems != {"common", mode}:
                problems.append(f"{adapter} does not load common + {mode} in {mode} mode (got {sorted(stems)})")
            for r in refs:
                if not (root / r).is_file():
                    problems.append(f"{adapter} names a missing reference {r}")
    if baseline_inv is None:
        problems.append("no recorded baseline inventory to compare against")
    elif not isinstance(baseline_inv.get("aggregate_bytes"), int) or isinstance(baseline_inv.get("aggregate_bytes"), bool):
        problems.append("the recorded baseline inventory has no whole-number aggregate_bytes")
    else:
        before = baseline_inv["aggregate_bytes"]
        after = inv["aggregate_bytes"]
        if before <= 0 or after > before * (1 - LOAD_REDUCTION):
            problems.append(
                f"mandatory scenario loads are {after:,} bytes against a baseline of "
                f"{before:,}: not {int(LOAD_REDUCTION * 100)}% smaller")
    doc_path = root / REPORT_DOC
    baseline_paragraphs = root / BASELINE_PARAGRAPHS
    if not doc_path.is_file():
        problems.append(f"{REPORT_DOC} is missing")
    elif not baseline_paragraphs.is_file():
        problems.append(f"{BASELINE_PARAGRAPHS} is missing; the preservation map cannot be checked")
    else:
        try:
            pmap = preservation_map(doc_path.read_text(encoding="utf-8"))
        except (ShipEfficiencyError, json.JSONDecodeError) as exc:
            problems.append(str(exc))
        else:
            found, edited = preservation_report(
                root, pmap, json.loads(baseline_paragraphs.read_text(encoding="utf-8")))
            problems.extend(found)
            if edited_out is not None:
                edited_out.extend(edited)
    return problems


# --- run evidence and the report --------------------------------------------

def load_runs(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or "runs" not in data:
        raise ShipEfficiencyError(f"{path}: not a run-evidence file (schema {SCHEMA})")
    return data


def usage_totals(role_records: list[dict]) -> dict:
    """One role's usage, each unique (provider, session, turn) once.

    ``per-turn`` records add; a ``cumulative`` record replaces the session's
    running total (the largest one wins, never the sum); any of the four
    token fields absent or null on any counted record makes that whole
    figure ``None`` — unknown, never zero — and a missing input or output
    figure also marks the role ``incomplete``, so no paired median is ever
    computed over it. A record flagged ``includes_children`` is a parent
    total that already contains its children, so it is kept apart under
    ``end_to_end``.
    """
    seen: set[tuple] = set()
    per_turn = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    cumulative: dict[tuple, dict] = {}
    unknown = {"input": False, "output": False, "cache_read": False, "cache_write": False}
    end_to_end = None
    incomplete = False
    for rec in role_records:
        key = (rec.get("provider"), rec.get("session"), rec.get("turn"))
        if key in seen:
            continue
        seen.add(key)
        if rec.get("incomplete"):
            incomplete = True
        if rec.get("includes_children"):
            end_to_end = {"input": rec.get("input_tokens"), "output": rec.get("output_tokens")}
            continue
        fields = {"input": rec.get("input_tokens"), "output": rec.get("output_tokens"),
                  "cache_read": rec.get("cache_read_tokens"), "cache_write": rec.get("cache_write_tokens")}
        for f, v in fields.items():
            if v is None:
                unknown[f] = True
        if rec.get("kind", "per-turn") == "cumulative":
            skey = (rec.get("provider"), rec.get("session"))
            prior = cumulative.get(skey)
            if prior is None or (fields["input"] or 0) > (prior["input"] or 0):
                cumulative[skey] = fields
        else:
            for f, v in fields.items():
                if v is not None:
                    per_turn[f] += v
    total = dict(per_turn)
    for fields in cumulative.values():
        for f, v in fields.items():
            if v is not None:
                total[f] += v
    for f, missing in unknown.items():
        if missing:
            total[f] = None
    if unknown["input"] or unknown["output"]:
        incomplete = True
    total["end_to_end"] = end_to_end
    total["incomplete"] = incomplete
    total["records_counted"] = len(seen)
    return total


def duplicate_loads(reads: list[dict]) -> list[dict]:
    """Paths one role read more than once."""
    counts: dict[tuple, int] = {}
    for r in reads:
        key = (r.get("role"), r.get("path"))
        counts[key] = counts.get(key, 0) + 1
    return [{"role": k[0], "path": k[1], "times": n} for k, n in sorted(counts.items()) if n > 1]


def command_fingerprint(command: dict) -> tuple:
    return (command.get("owner"), tuple(command.get("argv", [])), command.get("cwd"),
            command.get("env_id"), command.get("toolchain_id"), command.get("input_digest"))


def reusable(earlier: dict, later: dict) -> tuple[bool, str]:
    """May ``later`` cite ``earlier``'s execution instead of running again?

    Only inside one owner, on the same argv, cwd, environment, toolchain and
    complete input digest, with a zero exit and a log on disk. Any difference
    — a different role most of all — is a rerun.
    """
    if not earlier:
        return False, "no earlier execution to cite"
    if earlier.get("owner") != later.get("owner"):
        return False, "different owner: evidence is never shared across roles"
    if not earlier.get("input_digest") or not later.get("input_digest"):
        return False, "no provable input digest"
    if command_fingerprint(earlier) != command_fingerprint(later):
        for field in ("argv", "cwd", "env_id", "toolchain_id", "input_digest"):
            if earlier.get(field) != later.get(field):
                return False, f"{field} differs"
        return False, "fingerprint differs"
    if earlier.get("exit") != 0:
        return False, "the earlier run did not exit 0" if earlier.get("exit") is not None else "the earlier run was interrupted"
    if not earlier.get("log"):
        return False, "the earlier run kept no log"
    return True, "identical execution, same owner"


def command_accounting(commands: list[dict]) -> dict:
    executed = 0
    reused = 0
    duplicates: list[dict] = []
    seen: dict[tuple, dict] = {}
    full_suites: dict[str, int] = {}
    for c in commands:
        if c.get("cited"):
            # The execution a citation points at: the latest run of the same
            # argv in the same cwd, this owner's first — `reusable` then says
            # whether the citation holds.
            same = [e for k, e in seen.items() if k[1:3] == (tuple(c.get("argv", [])), c.get("cwd"))]
            own = [e for e in same if e.get("owner") == c.get("owner")]
            ok, why = reusable((own or same or [{}])[-1], c)
            if not ok:
                duplicates.append({"owner": c.get("owner"), "argv": c.get("argv"), "problem": f"cited an execution it may not reuse: {why}"})
            reused += 1
            continue
        executed += 1
        key = command_fingerprint(c)
        if key in seen and seen[key].get("exit") == 0 and seen[key].get("log"):
            duplicates.append({"owner": c.get("owner"), "argv": c.get("argv"), "problem": "identical execution repeated inside one role"})
        seen[key] = c
        if list(c.get("argv", []))[:1] == [".venv/bin/pytest"] and len(c.get("argv", [])) <= 2:
            full_suites[c.get("owner")] = full_suites.get(c.get("owner"), 0) + 1
    return {"executed": executed, "reused": reused, "duplicates": duplicates, "full_suites": full_suites}


def run_summary(run: dict) -> dict:
    roles = {}
    for role in run.get("roles", []):
        roles[role["role"]] = usage_totals(role.get("records", []))
    reads = run.get("reads", [])
    commands = run.get("commands", [])
    parent = next((r for r in roles.values() if r.get("end_to_end")), None)
    if parent and parent["end_to_end"]:
        end_to_end = dict(parent["end_to_end"])
    else:
        end_to_end = {
            f: (None if any(r[f] is None for r in roles.values())
                else sum(r[f] for r in roles.values()))
            for f in ("input", "output")}
    cost = run.get("cost")
    if cost is not None and not (isinstance(cost, dict) and cost.get("amount") is not None
                                 and cost.get("currency") and cost.get("provenance")):
        cost = {"unavailable": True, "reason": "no attributable amount with units and provenance"}
    return {
        "run_id": run.get("run_id"), "scenario": run.get("scenario"),
        "complete": bool(run.get("complete")) and not any(r["incomplete"] for r in roles.values()),
        "cache_state": run.get("cache_state", "unknown"),
        "roles": roles, "end_to_end": end_to_end,
        "read_bytes": sum(int(r.get("bytes", 0)) for r in reads),
        "duplicate_loads": duplicate_loads(reads),
        "commands": command_accounting(commands),
        "repairs": int(run.get("repairs", 0)),
        "elapsed_seconds": run.get("elapsed_seconds"),
        "cost": cost,
    }


def paired_reduction(baseline: list[dict], candidate: list[dict]) -> dict:
    """Median input reduction over pairs where **both** runs are complete
    and share a scenario and cache state; every other pair is listed as
    excluded, never averaged in. Each side of a pair is **one** run: a
    second run on the same scenario and cache state is a duplicate arm, and
    the pair is excluded rather than averaged or silently replaced."""
    by_key: dict[tuple, tuple[list, list]] = {}
    for run in baseline:
        by_key.setdefault((run["scenario"], run["cache_state"]), ([], []))[0].append(run)
    for run in candidate:
        by_key.setdefault((run["scenario"], run["cache_state"]), ([], []))[1].append(run)
    ratios: list[float] = []
    excluded: list[dict] = []
    for key, (bs, cs) in sorted(by_key.items(), key=lambda kv: str(kv[0])):
        if len(bs) > 1 or len(cs) > 1:
            excluded.append({"pair": list(key), "why": "duplicate arm"})
            continue
        if not bs or not cs:
            excluded.append({"pair": list(key), "why": "unpaired"})
            continue
        b, c = bs[0], cs[0]
        if not (b["complete"] and c["complete"]):
            excluded.append({"pair": list(key), "why": "incomplete usage"})
            continue
        bi, ci = b["end_to_end"]["input"], c["end_to_end"]["input"]
        if not bi or ci is None or ci == 0:
            excluded.append({"pair": list(key), "why": "no input figure"})
            continue
        ratios.append(1 - ci / bi)
    result = {"complete_pairs": len(ratios), "excluded": excluded,
              "median_input_reduction": statistics.median(ratios) if ratios else None}
    result["target"] = RUNTIME_TARGET
    result["target_met"] = (result["median_input_reduction"] is not None
                            and result["median_input_reduction"] >= RUNTIME_TARGET)
    return result


def report(baseline: dict, candidate: dict) -> dict:
    b = [run_summary(r) for r in baseline["runs"]]
    c = [run_summary(r) for r in candidate["runs"]]
    cold = paired_reduction([r for r in b if r["cache_state"] == "cold"],
                            [r for r in c if r["cache_state"] == "cold"])
    warm = paired_reduction([r for r in b if r["cache_state"] != "cold"],
                            [r for r in c if r["cache_state"] != "cold"])
    return {
        "schema": SCHEMA,
        "baseline": {"label": baseline.get("label", "baseline"), "runs": b},
        "candidate": {"label": candidate.get("label", "candidate"), "runs": c},
        "paired": {"cold": cold, "warm": warm},
        "note": ("Token fields are copied from the records as the provider defined them; "
                 "a null cache field is unknown, not zero; cost appears only with an "
                 "attributable amount, currency and provenance."),
    }


# --- the delegation ledgers ----------------------------------------------------

def shunt_records(ledgers: Path) -> list[dict]:
    """Every well-formed record under ``ledgers``, once per ``delegation_id``.
    A line that is not a JSON object, or carries no id, is skipped; a
    duplicate id — the same delegation appended twice — counts once, the
    first line winning. A ``note`` record with no id (the fixture's first
    line) is skipped the same way."""
    out: list[dict] = []
    seen: set[str] = set()
    for path in sorted(Path(ledgers).glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            ident = record.get("delegation_id")
            if not isinstance(ident, str) or not ident.strip() or ident in seen:
                continue
            seen.add(ident)
            out.append(dict(record, _ledger=path.name))
    return out


def _shunt_bucket(records: list[dict]) -> dict:
    """``delegations``, ``lines_kept_out`` and ``worker_cost`` over one
    group. The cost is a USD sum only when every record in the group
    carries a measured number; one missing figure makes the whole bucket
    ``unavailable`` — a partial sum would read as the cost where it is a
    floor. Never zero for an unknown. A record whose ``ok`` is false is a
    delegation that kept nothing out: it is counted, its lines are not."""
    lines = 0
    cost = 0.0
    known = bool(records)
    for record in records:
        kept = 0 if record.get("ok") is False else record.get("lines_kept_out")
        if isinstance(kept, bool) or not isinstance(kept, int):
            try:
                kept = int(kept)
            except (TypeError, ValueError):
                kept = 0
        lines += max(kept, 0)
        usd = record.get("worker_cost_usd")
        if isinstance(usd, bool) or not isinstance(usd, (int, float)):
            known = False
        else:
            cost += float(usd)
    return {
        "delegations": len(records),
        "lines_kept_out": lines,
        "worker_cost": ({"usd": round(cost, 4)} if known else "unavailable"),
    }


def shunt_report(ledgers: Path) -> dict:
    """The delegation report: the totals, then per provider and per mode,
    each through ``_shunt_bucket``. ``sessions`` counts ledger files, not
    cards — a sub-agent with its own session id is its own file, counted
    here and absent from any card. The note says in words what this is
    not: a saving, a token count, or anything about the main model."""
    records = shunt_records(ledgers)
    by_provider: dict[str, list[dict]] = {}
    by_mode: dict[str, list[dict]] = {}
    failed = 0
    for record in records:
        by_provider.setdefault(str(record.get("provider") or "unknown"), []).append(record)
        by_mode.setdefault(str(record.get("mode") or "unknown"), []).append(record)
        if record.get("ok") is False:
            failed += 1
    total = _shunt_bucket(records)
    return {
        "schema": SCHEMA,
        "ledgers": len({r["_ledger"] for r in records}),
        "delegations": total["delegations"],
        "lines_kept_out": total["lines_kept_out"],
        "worker_cost": total["worker_cost"],
        "failed": failed,
        "per_provider": {k: _shunt_bucket(v) for k, v in sorted(by_provider.items())},
        "per_mode": {k: _shunt_bucket(v) for k, v in sorted(by_mode.items())},
        "not_measured": [
            "the main model's context, input tokens or cost on any run",
            "what the same run would have read without the helper",
            "any percentage saved",
        ],
        "note": ("Counts of what the delegation ledgers recorded: lines the "
                 "wrappers sent to or received from the helper, never a "
                 "measurement of the main model. worker_cost is unavailable "
                 "unless every record carried a measured USD figure."),
    }


# --- the document's declarations ---------------------------------------------

def markers(doc_text: str) -> dict[str, list[dict[str, str]]]:
    """``<!-- ship-efficiency: kind key=value ... -->`` comments, by kind."""
    out: dict[str, list[dict[str, str]]] = {}
    for match in MARKER_RE.finditer(doc_text):
        words = match.group(1).split()
        if not words:
            continue
        kind, fields = words[0], {}
        for word in words[1:]:
            key, _, value = word.partition("=")
            fields[key] = value
        out.setdefault(kind, []).append(fields)
    return out


def _flat(text: str) -> str:
    return " ".join(text.split()).lower()


REQUIRED_EVIDENCE = (
    "scenario", "arm", "source", "redacted", "verdict", "seeded_faults",
    "caught", "clean_verdict_preserved", "gates_run", "models", "reasoning",
    "cross_role_pass_reuse", "verifier_report_leaked",
)


#: The closed set a record's ``verdict`` may take: the reviewers' SHIP /
#: ITERATE / STOP, the verifier's PASS / FAIL, and a review BLOCK.
VERDICTS = ("SHIP", "ITERATE", "STOP", "PASS", "FAIL", "BLOCK")
#: The two that mean "nothing stopped the work".
CLEAN_VERDICTS = ("SHIP", "PASS")
#: The scenarios that carry a planted fault; a clean verdict on one of
#: these is a miss, whichever arm produced it.
SEEDED_SCENARIOS = (3, 4, 5, 6)


def _scenario_number(value) -> int | None:
    """A marker's ``scenario=`` value as a whole number, or ``None`` when it
    is not one — never an exception out of ``int()``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value if value is not None else "").strip()
    return int(text) if text.isdigit() else None


def _names(value) -> list[str]:
    """A record's list field as a list of strings; anything else is empty,
    so a wrong shape reads as "nothing" and the non-empty rules refuse it."""
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, (str, int)) and str(v).strip()]


def _record_problems(n: int, arm: str, record, allow_synthetic: bool) -> list[str]:
    """Everything wrong with one arm's record on its own. Vacuous evidence
    is refused: every field must say something a person could have
    observed, and a record that calls itself synthetic is never evidence at
    the shipped path."""
    who = f"scenario {n} {arm}"
    if not isinstance(record, dict):
        return [f"{who}: evidence is not a record"]
    missing = [k for k in REQUIRED_EVIDENCE if k not in record]
    if missing:
        return [f"{who}: evidence lacks {missing}"]
    out: list[str] = []
    source = record["source"]
    if record.get("synthetic") and not allow_synthetic:
        out.append(f"{who}: evidence is marked synthetic and is not an observed run")
    if isinstance(source, str) and source.startswith("synthetic://") and not allow_synthetic:
        out.append(f"{who}: evidence names a synthetic source, not an observed run")
    if not record["redacted"]:
        out.append(f"{who}: evidence is not marked redacted")
    if not source or not isinstance(source, str):
        out.append(f"{who}: evidence names no source")
    if record["verdict"] not in VERDICTS:
        out.append(f"{who}: verdict must be one of {', '.join(VERDICTS)}, got {record['verdict']!r}")
    elif n in SEEDED_SCENARIOS and record["verdict"] in CLEAN_VERDICTS:
        out.append(f"{who}: a seeded-fault scenario ended {record['verdict']}; the planted fault did not stop the work")
    elif n not in SEEDED_SCENARIOS and record["verdict"] not in CLEAN_VERDICTS:
        out.append(f"{who}: a clean scenario ended {record['verdict']}; a problem was invented where none was planted")
    if n not in SEEDED_SCENARIOS and record["verdict"] in VERDICTS:
        # The flag is a restatement of the verdict, never a second truth: a
        # record may not call a clean verdict preserved while ending ITERATE,
        # nor disown a SHIP it recorded.
        preserved = record["verdict"] in CLEAN_VERDICTS
        if bool(record["clean_verdict_preserved"]) != preserved:
            out.append(f"{who}: clean_verdict_preserved is {record['clean_verdict_preserved']!r} "
                       f"but the verdict is {record['verdict']}; the flag must restate the verdict")
    if n in SEEDED_SCENARIOS and not _names(record["seeded_faults"]):
        out.append(f"{who}: a seeded-fault scenario names no seeded fault")
    if not _names(record["gates_run"]):
        out.append(f"{who}: no gate ran")
    if not record["models"] or not isinstance(record["models"], dict):
        out.append(f"{who}: models are not stated")
    if not record["reasoning"] or not isinstance(record["reasoning"], str):
        out.append(f"{who}: reasoning is not stated")
    if record["cross_role_pass_reuse"]:
        out.append(f"{who}: a pass was reused across roles")
    if record["verifier_report_leaked"]:
        out.append(f"{who}: the verifier saw the implementer's report")
    return out


def validate_evaluation(doc_text: str, fixtures: Path, allow_synthetic: bool = False) -> list[str]:
    """Problems with the paired evaluation; an empty list means all six
    scenarios ran, their twelve records are on disk and every quality rule
    holds. A missing run is a problem, never a pass, and so is vacuous
    evidence: a record marked synthetic, an empty gate list, unstated
    models or reasoning, a verdict outside ``VERDICTS``, a seeded-fault
    scenario with no seeded fault or a clean verdict, or a clean scenario
    (1–2) that ended outside ``CLEAN_VERDICTS`` or whose
    ``clean_verdict_preserved`` flag contradicts its verdict — the record's
    verdict is read, never the flag alone. ``allow_synthetic``
    exists for this tool's own tests and has no command-line switch, so the
    shipped check can never pass on the synthetic set."""
    problems: list[str] = []
    decl = markers(doc_text)
    scenarios = []
    numbers: list[int] = []
    for s in decl.get("evaluation", []):
        n = _scenario_number(s.get("scenario"))
        if n is None:
            problems.append(f"an evaluation marker names no whole-number scenario: {s.get('scenario')!r}")
            continue
        numbers.append(n)
        scenarios.append(dict(s, scenario=n))
    if sorted(numbers) != [1, 2, 3, 4, 5, 6]:
        problems.append(f"the document must declare all six scenarios once; found {sorted(numbers)}")
    flat = _flat(doc_text)
    missing_runs: list[str] = []
    for s in scenarios:
        n = s["scenario"]
        status = s.get("status")
        if status == "not-run":
            present = [f"scenario-{n}-{arm}.json" for arm in ("baseline", "candidate")
                       if (fixtures / f"scenario-{n}-{arm}.json").is_file()]
            if present:
                missing_runs.append(f"scenario {n}: not run (declared not-run, yet {' / '.join(present)} "
                                    f"exist under {fixtures}: declare it ran or remove them)")
            else:
                missing_runs.append(f"scenario {n}: not run (declared not-run; no "
                                    f"scenario-{n}-baseline.json / scenario-{n}-candidate.json under {fixtures})")
            if "not run" not in flat and "not measured" not in flat and "were not run" not in flat:
                problems.append(f"scenario {n} is declared not-run but the prose never says so")
            if s.get("claims", "none") != "none":
                problems.append(f"scenario {n} is not-run and may claim nothing")
            continue
        if status != "ran":
            problems.append(f"scenario {n}: status must be ran or not-run, got {status!r}")
            continue
        for arm in ("baseline", "candidate"):
            path = fixtures / f"scenario-{n}-{arm}.json"
            if not path.is_file():
                missing_runs.append(f"scenario {n}: declared ran but {path.name} is missing under {fixtures}")
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                problems.append(f"scenario {n} {arm}: unreadable evidence ({exc})")
                continue
            problems.extend(_record_problems(n, arm, record, allow_synthetic))
        b = fixtures / f"scenario-{n}-baseline.json"
        c = fixtures / f"scenario-{n}-candidate.json"
        if b.is_file() and c.is_file():
            try:
                bl = json.loads(b.read_text(encoding="utf-8"))
                cd = json.loads(c.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if not isinstance(bl, dict) or not isinstance(cd, dict):
                continue
            seeded = set(_names(bl.get("seeded_faults"))) | set(_names(cd.get("seeded_faults")))
            if seeded - set(_names(cd.get("caught"))):
                problems.append(f"scenario {n}: the candidate missed a seeded fault")
            if set(_names(bl.get("caught"))) - set(_names(cd.get("caught"))):
                problems.append(f"scenario {n}: the candidate missed a finding the baseline caught")
            if bl.get("clean_verdict_preserved") and not cd.get("clean_verdict_preserved"):
                problems.append(f"scenario {n}: the candidate did not preserve the clean verdict")
            if set(_names(bl.get("gates_run"))) - set(_names(cd.get("gates_run"))):
                problems.append(f"scenario {n}: the candidate skipped a required gate")
            if bl.get("models") != cd.get("models") or bl.get("reasoning") != cd.get("reasoning"):
                problems.append(f"scenario {n}: models or reasoning differ between arms")
    claim = {c.get("key"): c.get("value") for c in decl.get("claim", []) if "key" in c}
    default = claim.get("reduced_default")
    if missing_runs:
        problems.extend(missing_runs)
        problems.append(f"the paired evaluation is incomplete: {len(missing_runs)} missing run(s) "
                        "named above; the quality half of the acceptance is not demonstrated")
        if default != "pending":
            problems.append("with runs missing the reduced default must be declared "
                            f"reduced_default=pending, not {default!r}")
        if "pending" not in flat:
            problems.append("with runs missing the prose must say the reduced default is pending the evaluation")
    elif problems and default == "on":
        problems.append("a scenario missed its mark, so the reduced default cannot be declared on")
    elif default not in ("pending", "on", "off"):
        problems.append(f"reduced_default must be pending, on or off, got {default!r}")
    return problems


def validate_report(doc_text: str, root: Path | None = None) -> list[str]:
    problems: list[str] = []
    for heading in ("## Static measurements", "## Observed usage", "## Delegation", "## Money"):
        if heading not in doc_text:
            problems.append(f"the report lacks the section {heading!r}")
    decl = markers(doc_text)
    claims = {c.get("key"): c.get("value") for c in decl.get("claim", []) if "key" in c}
    flat = _flat(doc_text)
    for key in ("runtime_input_reduction", "cost_saving", "coverage", "usage_missing", "shunt"):
        if key not in claims:
            problems.append(f"the report declares no claim for {key}")
    shunt = claims.get("shunt")
    if shunt == "unmeasured":
        if "not measured" not in flat:
            problems.append("shunt=unmeasured but the prose never says 'not measured'")
    elif shunt == "measured":
        results = claims.get("shunt_results")
        if not results or root is None or not (root / results).is_file():
            problems.append("a measured shunt claim must name a results file computed by `shunt`")
    elif shunt is not None:
        problems.append(f"shunt must be measured or unmeasured, got {shunt!r}")
    runtime = claims.get("runtime_input_reduction")
    if runtime == "met":
        results = claims.get("results")
        if not results or root is None or not (root / results).is_file():
            problems.append("a met runtime target must name a results file computed by `report`")
        else:
            data = json.loads((root / results).read_text(encoding="utf-8"))
            cold = data.get("paired", {}).get("cold", {})
            if not cold.get("complete_pairs") or not cold.get("target_met"):
                problems.append("the results file does not show the target met over complete cold pairs")
    elif runtime in ("not-met", "unmeasured"):
        phrase = "not met" if runtime == "not-met" else "not measured"
        if phrase not in flat:
            problems.append(f"runtime_input_reduction={runtime} but the prose never says '{phrase}'")
        if claims.get("cost_saving") != "none":
            problems.append("no cost saving may be claimed without a measured runtime reduction")
    else:
        problems.append(f"runtime_input_reduction must be met, not-met or unmeasured, got {runtime!r}")
    if claims.get("cost_saving") == "none":
        for bad in ("saves $", "cost saving of", "% cheaper", "reduces the bill"):
            if bad in flat:
                problems.append(f"the report claims no cost saving yet says {bad!r}")
    if claims.get("usage_missing") == "yes" and "unavailable" not in flat and "not measured" not in flat:
        problems.append("usage is declared missing but the prose never says so")
    if claims.get("coverage") not in ("static", "static+runtime"):
        problems.append("coverage must be declared static or static+runtime")
    return problems


# --- the command line ----------------------------------------------------------

def _print_problems(label: str, problems: list[str]) -> int:
    if problems:
        print(f"{label}: {len(problems)} problem(s)")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"{label}: ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("inventory", help="measure the instruction load sets")
    inv.add_argument("--root", default=".")
    inv.add_argument("--output")
    inv.add_argument("--check", action="store_true")
    inv.add_argument("--baseline", help=f"recorded baseline inventory (default {BASELINE_INVENTORY})")
    inv.add_argument("--source", help="where the measured tree came from, in words; written as 'source'")

    rep = sub.add_parser("report", help="aggregate run evidence")
    rep.add_argument("--baseline", required=True)
    rep.add_argument("--candidate", required=True)
    rep.add_argument("--output", required=True)

    ev = sub.add_parser("validate-evaluation", help="check the paired evaluation's evidence")
    ev.add_argument("doc")
    ev.add_argument("--fixtures", required=True)

    vr = sub.add_parser("validate-report", help="check the report's claims")
    vr.add_argument("doc")

    sh = sub.add_parser("shunt", help="count what the delegation ledgers recorded")
    sh.add_argument("--ledgers", required=True, help="a directory of <session id>.jsonl files")
    sh.add_argument("--output")

    bp = sub.add_parser("baseline-paragraphs", help="record an old root's paragraph digests")
    bp.add_argument("file")
    bp.add_argument("--output", required=True)
    bp.add_argument("--source", required=True, help="where these bytes came from, in words")

    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            root = Path(args.root).resolve()
            result = inventory(root)
            if args.source:
                result["source"] = args.source
            if args.output:
                Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                print(f"inventory written to {args.output} ({result['aggregate_bytes']:,} bytes across scenarios)")
            if args.check:
                baseline_path = Path(args.baseline) if args.baseline else root / BASELINE_INVENTORY
                baseline_inv = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.is_file() else None
                if baseline_inv is not None and not isinstance(baseline_inv, dict):
                    raise ShipEfficiencyError(f"{baseline_path}: not a baseline inventory")
                edited: list[str] = []
                problems = check_inventory(root, result, baseline_inv, edited)
                before = baseline_inv.get("aggregate_bytes") if baseline_inv is not None else None
                if isinstance(before, int) and not isinstance(before, bool) and before > 0:
                    after = result["aggregate_bytes"]
                    print(f"scenario loads: {before:,} -> {after:,} bytes ({(1 - after / before) * 100:.1f}% smaller)")
                for note in edited:
                    print(f"note: {note}")
                return _print_problems("inventory --check", problems)
            if not args.output:
                print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "report":
            result = report(load_runs(Path(args.baseline)), load_runs(Path(args.candidate)))
            Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            cold = result["paired"]["cold"]
            print(f"report written to {args.output}: {cold['complete_pairs']} complete cold pair(s), "
                  f"median input reduction {cold['median_input_reduction']}")
            return 0
        if args.command == "validate-evaluation":
            text = Path(args.doc).read_text(encoding="utf-8")
            return _print_problems("validate-evaluation", validate_evaluation(text, Path(args.fixtures)))
        if args.command == "validate-report":
            doc = Path(args.doc)
            root = doc.resolve().parent.parent
            return _print_problems("validate-report", validate_report(doc.read_text(encoding="utf-8"), root))
        if args.command == "shunt":
            ledgers = Path(args.ledgers)
            if not ledgers.is_dir():
                raise ShipEfficiencyError(f"{ledgers}: not a directory of ledgers")
            result = shunt_report(ledgers)
            text = json.dumps(result, indent=2, sort_keys=True) + "\n"
            if args.output:
                Path(args.output).write_text(text, encoding="utf-8")
                cost = result["worker_cost"]
                print(f"shunt report written to {args.output}: {result['delegations']} delegation(s), "
                      f"{result['lines_kept_out']:,} lines kept out, worker cost "
                      f"{cost if isinstance(cost, str) else '$' + str(cost['usd'])}")
            else:
                print(text, end="")
            return 0
        if args.command == "baseline-paragraphs":
            result = baseline_paragraphs(Path(args.file).read_text(encoding="utf-8"), args.source)
            Path(args.output).write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"{len(result['paragraphs'])} paragraphs ({result['bytes']:,} bytes) written to {args.output}")
            return 0
    except (ShipEfficiencyError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (KeyError, ValueError, TypeError) as exc:
        # An input file of the wrong shape — a missing key, a figure that is
        # not a number — is a named problem and exit 2, never a traceback.
        print(f"error: input of the wrong shape ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
