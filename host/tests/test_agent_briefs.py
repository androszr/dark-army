"""The crew's roster agreement, and the preparer's sentence in the copies
nothing else pins.

Two disagreements are live in this tree and this file refuses both coming back:

1. **An agent file the board's crew tiles have never heard of.** The board draws
   a face per stage from `Specialists.table`
   (`panel/Sources/BobPanel/Specialists.swift`); an agent added under
   `.claude/agents/` with no entry there draws a hashed stranger. Both directions
   are asserted, and the reverse one carries `PENDING_IN_TABLE`, an allowlist the
   third test forces to be emptied the day it stops being true.
2. **The preparer's sentence differing between its copies.** The `.md`
   frontmatter half is already pinned against `card_preparer_brief.DESCRIPTION`
   by `test_card_preparer_brief.py::test_repo_description_matches_the_shipped_constant`
   and is deliberately **not** duplicated here. What nothing pinned — and what
   was stale until `plans/2026-09-06-agent-roles-in-plain-words.md` — are the two
   sidecars the other two assistants read.

The individual role *strings* in `Specialists.table` are deliberately not pinned
to the agent files' prose: coupling a four-word Swift caption to a Markdown
sentence would make every wording tweak a two-file edit. This pins the roster,
not the words.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dark_army_daemon import card_preparer_brief

REPO = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO / ".claude" / "agents"
SPECIALISTS = REPO / "panel" / "Sources" / "BobPanel" / "Specialists.swift"

#: Stages that have an agent file but no `Specialists.table` entry yet.
#:
#: **Empty, and it has to stay that way to mean anything.** It exists as the one
#: sanctioned way to land an agent file ahead of its table entry, named together
#: with the plan that will add it — and `test_the_pending_set_retires_itself`
#: fails the moment a name in here reaches the table, so the allowlist cannot
#: outlive its reason. It held `bc-card-preparer` and `bc-security-reviewer`
#: until `plans/2026-09-06-card-crew-of-agents.md` added both keys.
PENDING_IN_TABLE: set[str] = set()


def _table_stages() -> set[str]:
    """The stage keys of the panel's `Specialists.table` literal.

    Parsed out of the Swift source rather than mirrored in Python: a copy here
    would agree with itself and pin nothing.
    """
    text = SPECIALISTS.read_text(encoding="utf-8")
    marker = "static let table: [String: Known] = ["
    start = text.index(marker) + len(marker)
    end = text.index("]", start)
    return set(re.findall(r'"(bc-[a-z-]+)":\s*Known\(', text[start:end]))


def _agent_stems() -> set[str]:
    return {path.stem for path in AGENT_DIR.glob("*.md")}


def test_every_specialist_stage_has_an_agent_file():
    """A face on a card must name a helper that exists."""
    stages = _table_stages()
    assert stages, (
        f"parsed no stages out of {SPECIALISTS.name}; the literal was refactored "
        "and this parser needs updating rather than deleting"
    )
    missing = sorted(
        stage for stage in stages if not (AGENT_DIR / f"{stage}.md").is_file()
    )
    assert not missing, (
        f"Specialists.table names {missing} but .claude/agents/ has no such file"
    )


def test_every_agent_file_has_a_specialist_stage():
    """A helper the board's crew tiles have never heard of draws a stranger."""
    stages = _table_stages()
    stems = _agent_stems()
    assert stems, "no agent files found under .claude/agents/"
    orphans = sorted(stems - stages - PENDING_IN_TABLE)
    assert not orphans, (
        f"{orphans} have agent files but no Specialists.table entry. Add the "
        "stage to panel/Sources/BobPanel/Specialists.swift (and its byte-mirror "
        "in ios/BobPhone/Specialists.swift), or name it in PENDING_IN_TABLE with "
        "the plan that will."
    )


def test_the_pending_set_retires_itself():
    """The allowlist may not outlive the reason it was written for."""
    stages = _table_stages()
    landed = sorted(name for name in PENDING_IN_TABLE if name in stages)
    assert not landed, (
        f"{landed} are in Specialists.table now; remove them from "
        "PENDING_IN_TABLE in the same change"
    )


SIDECARS = (
    (Path(".codex/agents/bc-card-preparer.toml"), r'description = "(.*)"'),
    (Path(".grok/agents/bc-card-preparer.md"), r'description: "(.*)"'),
)


# Local shims are hand-maintained; generated pack parity cannot protect them.
def _check_local_codex_roster(repo: Path) -> None:
    import tomllib

    briefs = repo / '.claude/agents'
    shims = repo / '.codex/agents'
    declared = {}
    for brief in briefs.glob('*.md'):
        frontmatter = brief.read_text().split('---', 2)[1]
        match = re.search(r'^name: (bc-[a-z-]+)$', frontmatter, re.M)
        assert match, f'{brief.name}: missing canonical name'
        assert match[1] == brief.stem, f'{brief.name}: canonical name mismatch'
        declared[match[1]] = brief
    assert len(declared) == 7, 'expected all seven local canonical roles'
    assert {path.stem for path in shims.glob('*.toml')} == set(declared), 'local roster mismatch'
    for role, brief in declared.items():
        data = tomllib.loads((shims / f'{role}.toml').read_text())
        assert data['name'] == role, f'{role}: canonical name mismatch'
        expected = 'workspace-write' if role in {'bc-planner', 'bc-implementer'} else 'read-only'
        assert data['sandbox_mode'] == expected, f'{role}: sandbox must be {expected}'
        assert data['model_reasoning_effort'] == 'high', f'{role}: reasoning contract'
        # The model is the Agent models setting's, pinned in place by
        # `pack_install.pin_own_checkout` (22 Sep 2026); when present it is
        # one that setting allows for this role, never a hand-typed name.
        if 'model' in data:
            from dark_army_daemon import agent_models
            slot = role.removeprefix('bc-')
            assert data['model'] in agent_models.allowed('codex', slot), f'{role}: unknown model'
        instructions = data['developer_instructions']
        for reference in ('AGENTS.md', 'CLAUDE.md', str(brief.relative_to(repo))):
            assert reference in instructions, f'{role}: missing {reference}'
            assert (repo / reference).is_file(), f'{role}: nonexistent {reference}'
        assert 'completely before working' in instructions
        assert 'authoritative role specification' in instructions
        if role != 'bc-card-preparer':
            # The load rule: compact root, the brief, then the mapped subject
            # documents. The to-do file is retired (22 Sep 2026), so no shim
            # names one. The preparer keeps its own contract.
            assert 'docs/agent-context.json' in instructions, f'{role}: shim must name the context map'
            assert 'TODO' not in instructions, f'{role}: shim must not name a to-do file'
        if role == 'bc-implementer':
            assert 'ship-attempts.json' in instructions, f'{role}: shim must name the attempt ledger'
        if role in {'bc-bug-auditor', 'bc-integration-reviewer', 'bc-security-reviewer'}:
            assert 'In scope' in instructions, f'{role}: shim must name the scope mark'


def test_all_local_codex_roles_match_authoritative_briefs():
    _check_local_codex_roster(REPO)


def test_all_local_grok_roles_point_at_the_markdown_briefs():
    briefs = REPO / ".claude/agents"
    shims = REPO / ".grok/agents"
    declared = {path.stem for path in briefs.glob("bc-*.md")}
    assert {path.stem for path in shims.glob("bc-*.md")} == declared
    for role in declared:
        text = (shims / f"{role}.md").read_text(encoding="utf-8")
        assert f"name: {role}" in text, role
        assert f".claude/agents/{role}.md" in text, role
        assert "authoritative role specification" in text, role
        # The capability line is the shim's whole sandbox on Grok, so it has
        # to say what the Codex shim's sandbox_mode says for the same role.
        capability = re.search(r"^Capability for this role: \*\*(.+?)\*\*", text, re.M)
        assert capability, f"{role}: no capability line"
        line = capability[1]
        if role in {"bc-planner", "bc-implementer"}:
            assert line.startswith("edit the checkout"), (role, line)
            assert "never commit or push" in line, (role, line)
        else:
            assert line.startswith("read-only"), (role, line)
            assert "never edit a file" in line, (role, line)


@pytest.mark.parametrize('mutation', ['missing-security', 'writable-security', 'wrong-brief', 'unmapped-load'])
def test_local_codex_roster_rejects_broken_shims(tmp_path, mutation):
    import shutil

    for folder in ('.claude/agents', '.codex/agents'):
        shutil.copytree(REPO / folder, tmp_path / folder)
    for name in ('AGENTS.md', 'CLAUDE.md'):
        shutil.copyfile(REPO / name, tmp_path / name)
    _check_local_codex_roster(tmp_path)
    shim = tmp_path / '.codex/agents/bc-security-reviewer.toml'
    if mutation == 'missing-security':
        shim.unlink()
    elif mutation == 'writable-security':
        shim.write_text(shim.read_text().replace('read-only', 'workspace-write'))
    elif mutation == 'unmapped-load':
        shim.write_text(shim.read_text().replace('docs/agent-context.json', 'your own judgment'))
    else:
        shim.write_text(shim.read_text().replace('bc-security-reviewer.md', 'bc-verifier.md'))
    with pytest.raises(AssertionError):
        _check_local_codex_roster(tmp_path)


_LEAD_SLUGS = ["backbone", "desk", "pocket", "ledger", "play", "conductor", "gate", "universal"]
_PACK_LEADS = REPO / "host/dark_army_menubar/agent_pack/template/.claude/leads"


def _pool_line(text):
    return [line for line in text.splitlines() if line.startswith("Lead pool")]


@pytest.mark.parametrize("slug", _LEAD_SLUGS)
def test_delivery_lead_brief_is_plain_and_pack_copy_is_project_neutral(slug):
    """Dark Army's own lead briefs name its contracts; the pack's copies go
    into other people's projects, so they name none of Dark Army's files and
    keep the same shape and the same lead pool (22 Sep 2026)."""
    own = (REPO / ".claude/leads" / f"{slug}.md").read_text()
    pack = (_PACK_LEADS / f"{slug}.md").read_text()
    for text in (own, pack):
        assert not text.startswith("---")
        assert 5 <= len(re.findall(r"^- ", text, re.M)) <= 8
        assert not re.search(r"\bDA\b", text)
    assert _pool_line(pack) == _pool_line(own)
    assert "Dark Army" not in pack
    assert not re.search(r"docs/[a-z-]+-contract\.md", pack)
    assert "CLAUDE.md" not in pack
    assert own.splitlines()[0].split(" — ")[0] == pack.splitlines()[0].split(" — ")[0]


#: The bounded fix loop, phrase by phrase: the ledger file, the counted attempt
#: (attempt 0 is the first run), the per-gate budget, the same-failure stop, the
#: baseline check and its worktree recipe, and the full suite's once-then-once-more
#: order. Every phrase must survive in the authoritative brief **and** the pack
#: template, or a rendered project drifts back to an uncounted loop.
BOUNDED_LOOP_RULES = (
    "ship-attempts.json",
    "attempt 0",
    "three attempts",
    "two consecutive attempts",
    "PRE-EXISTING",
    "git worktree add",
    "once at the end",
    "at most once more",
    "never start a fourth",
)

#: The sentences the bounded loop replaced. "at most one retry cycle" was not
#: countable; "or you have broken something" ordered a fix of baseline failures
#: that were never the implementer's; the `test_statusline` parenthetical was
#: stale the day it was written.
BOUNDED_LOOP_BANNED = (
    "at most one retry cycle",
    "or you have broken something",
    "test_statusline",
)

IMPLEMENTER_BRIEFS = (
    Path(".claude/agents/bc-implementer.md"),
    Path("host/dark_army_menubar/agent_pack/template/.claude/agents/{{P}}-implementer.md"),
)


def _check_bounded_loop_brief(text: str) -> None:
    flat = " ".join(text.split())
    for rule in BOUNDED_LOOP_RULES:
        assert rule in flat, f"bounded-loop rule missing: {rule!r}"
    for banned in BOUNDED_LOOP_BANNED:
        assert banned not in flat, f"retired sentence is back: {banned!r}"


@pytest.mark.parametrize("relative", IMPLEMENTER_BRIEFS, ids=[str(p) for p in IMPLEMENTER_BRIEFS])
def test_implementer_brief_bounds_its_fix_loop(relative):
    """Both implementer briefs count their attempts and stop; neither says "one retry"."""
    _check_bounded_loop_brief((REPO / relative).read_text(encoding="utf-8"))


# --- findings outside the plan ------------------------------------------------------

#: The scope mark, phrase by phrase: the yes/no column, what it is judged
#: against, the test for an out-of-scope fix, the implementer's half of the
#: rule, the block that lists the out-of-scope rows and the word that sends a
#: serious one to the person. Every checker brief — local and pack — carries
#: all of them, or a fix round is spent on work the plan ruled out.
SCOPE_MARK_RULES = (
    "In scope: yes", "In scope: no", "`## Out of scope`", "acceptance criteria",
    "add a guarantee, subsystem or abstraction", "never answers its own finding",
    "OUT OF SCOPE:", "ESCALATE",
)
SCOPE_MARK_BRIEFS = (
    Path(".claude/agents/bc-bug-auditor.md"),
    Path(".claude/agents/bc-integration-reviewer.md"),
    Path(".claude/agents/bc-security-reviewer.md"),
    Path("host/dark_army_menubar/agent_pack/template/.claude/agents/{{P}}-bug-auditor.md"),
    Path("host/dark_army_menubar/agent_pack/profiles/web-next-vercel/agents/{{P}}-security-reviewer.md"),
    Path("host/dark_army_menubar/agent_pack/profiles/ios-swift-testflight/agents/{{P}}-app-reviewer.md"),
)
#: The auditor's header keeps its `#`, `Cat` and `Sev` columns before the mark;
#: a reviewer's header opens on `Sev`. Either way the column appears once.
SCOPE_HEADER = re.compile(r"\| (#\s*\| Cat \| Sev \| )?(Sev \| )?In scope \|")
AUDITOR_ONLY = ("in-scope rows only", "never drive an iteration")
REVIEWER_ONLY = ("in-scope findings only",)


def _check_scope_mark_brief(text: str, auditor: bool) -> None:
    flat = " ".join(text.split())
    for rule in SCOPE_MARK_RULES:
        assert rule in flat, f"scope rule missing: {rule!r}"
    headers = SCOPE_HEADER.findall(flat)
    assert len(headers) == 1, f"the In scope column must appear exactly once, found {len(headers)}"
    for phrase in (AUDITOR_ONLY if auditor else REVIEWER_ONLY):
        assert phrase in flat, f"scope rule missing: {phrase!r}"


@pytest.mark.parametrize("relative", SCOPE_MARK_BRIEFS, ids=[str(p) for p in SCOPE_MARK_BRIEFS])
def test_reviewer_brief_marks_scope(relative):
    """Every checker says of each finding whether it is inside the plan, scores
    or reaches its verdict over in-scope findings only, and lists the rest."""
    _check_scope_mark_brief((REPO / relative).read_text(encoding="utf-8"),
                            auditor="bug-auditor" in relative.name)


def _scope_paragraph(text: str) -> str:
    start = text.index("Every finding carries")
    end = text.index("the orchestrator acts.", start) + len("the orchestrator acts.")
    return text[start:end]


def test_the_scope_paragraph_is_byte_identical_across_the_six_briefs():
    """One rule, six copies: the auditor's wording is the reviewers' and the
    pack's, so no copy can drift into judging scope differently."""
    paragraphs = {str(p): _scope_paragraph((REPO / p).read_text(encoding="utf-8")) for p in SCOPE_MARK_BRIEFS}
    canonical = paragraphs[str(SCOPE_MARK_BRIEFS[0])]
    flat = " ".join(canonical.split())
    assert "*Reading the diff* below still wins" in flat and "is not a finding at all" in flat
    for name, paragraph in paragraphs.items():
        assert paragraph == canonical, name


# --- delegating big reads and boilerplate -----------------------------------------

DELEGATION_HEADING = "## Delegating big reads and boilerplate"

#: The three sentences every *writer* brief carries, whitespace-folded. A
#: read-only brief carries none of them: a reviewer edits no file, so it has no
#: boilerplate to write, and a review conducted through a helper's summary is
#: not a review — the section in those briefs is the exemption alone.
DELEGATION_SENTENCES = (
    "**Bulk-read** when surveying, answering *where is X handled*, or reading "
    "generated or fixture files: `python3 .claude/skills/shunt/bulk_read.py "
    "--question '…' <files>`.",
    "**Code-write** a *new* test file or module that must match a named exemplar: "
    "`python3 .claude/skills/shunt/code_write.py --spec '…' --reference <exemplar> "
    "--out <new path>`.",
    "**Never** for an edit (read the exact section with `sed -n`), for debugging, "
    "or for anything security-relevant; a delegation costs 10–30 s, so never for "
    "a file under the threshold.",
)
#: The fourth sentence, reviewers only.
DELEGATION_EXEMPTION = (
    "You are exempt from the guard by role; if a read is refused anyway, run "
    "`python3 .claude/skills/shunt/exempt.py on` and read the file whole — a "
    "reviewer reads by itself, never through a summary.")

PACK_AGENTS = Path("host/dark_army_menubar/agent_pack/template/.claude/agents")
WRITER_BRIEFS = (
    Path(".claude/agents/bc-planner.md"),
    Path(".claude/agents/bc-implementer.md"),
    PACK_AGENTS / "{{P}}-planner.md",
    PACK_AGENTS / "{{P}}-implementer.md",
)
REVIEWER_BRIEFS = (
    Path(".claude/agents/bc-verifier.md"),
    Path(".claude/agents/bc-bug-auditor.md"),
    Path(".claude/agents/bc-integration-reviewer.md"),
    Path(".claude/agents/bc-security-reviewer.md"),
    PACK_AGENTS / "{{P}}-verifier.md",
    PACK_AGENTS / "{{P}}-bug-auditor.md",
)


def _delegation_section(text: str) -> str:
    assert DELEGATION_HEADING in text, "the delegation section is missing"
    body = text.split(DELEGATION_HEADING, 1)[1]
    body = re.split(r"^## ", body, 1, flags=re.M)[0]
    return " ".join(body.split())


#: What a read-only brief must never say: a delegated write, a delegated
#: survey, or the helper scripts that perform either. `exempt.py` is the one
#: shunt script such a brief may name.
READ_ONLY_FORBIDDEN = ("Bulk-read", "Code-write", "bulk_read.py", "code_write.py")


def _check_reviewer_delegation(text: str) -> None:
    section = _delegation_section(text)
    assert DELEGATION_EXEMPTION in section
    for word in READ_ONLY_FORBIDDEN:
        assert word not in text, word


@pytest.mark.parametrize("relative", WRITER_BRIEFS, ids=[str(p) for p in WRITER_BRIEFS])
def test_every_writer_brief_carries_the_delegation_section_with_identical_sentences(relative):
    section = _delegation_section((REPO / relative).read_text(encoding="utf-8"))
    for sentence in DELEGATION_SENTENCES:
        assert sentence in section, (relative, sentence)
    assert section.count("**Bulk-read**") == 1
    assert "exempt from the guard" not in section, relative


@pytest.mark.parametrize("relative", REVIEWER_BRIEFS, ids=[str(p) for p in REVIEWER_BRIEFS])
def test_a_read_only_brief_carries_the_exemption_and_no_delegation(relative):
    """A verifier, auditor or reviewer never edits a file and never reads
    through a summary, so its section is the exemption paragraph alone — no
    Bulk-read, no Code-write, neither helper script named anywhere in the
    brief."""
    _check_reviewer_delegation((REPO / relative).read_text(encoding="utf-8"))


def test_the_delegation_section_is_byte_identical_across_local_and_pack_copies():
    for local, pack in (("bc-planner", "{{P}}-planner"), ("bc-implementer", "{{P}}-implementer"),
                        ("bc-verifier", "{{P}}-verifier"), ("bc-bug-auditor", "{{P}}-bug-auditor")):
        a = _delegation_section((REPO / ".claude/agents" / f"{local}.md").read_text(encoding="utf-8"))
        b = _delegation_section((REPO / PACK_AGENTS / f"{pack}.md").read_text(encoding="utf-8"))
        assert a == b, local


@pytest.mark.parametrize("role", ["bc-planner", "bc-implementer", "bc-verifier",
                                  "bc-bug-auditor", "bc-integration-reviewer",
                                  "bc-security-reviewer"])
def test_every_codex_shim_names_the_delegation_section(role):
    import tomllib
    data = tomllib.loads((REPO / ".codex/agents" / f"{role}.toml").read_text(encoding="utf-8"))
    instructions = data["developer_instructions"]
    assert DELEGATION_HEADING[3:] in instructions
    if role in ("bc-planner", "bc-implementer"):
        assert "bulk_read.py" in instructions and "code_write.py" in instructions
        assert "never an edit" in instructions
    else:
        assert "exempt.py on" in instructions and "never through a summary" in instructions


