"""The compact root, the context map and the split ship workflow.

Static coverage and budgets for `plans/2026-09-19-ship-token-efficiency.md`:
the old→new preservation map resolves, every reference exists and escapes
nothing, every role loads the compact core, a mode change loads the right
reference, uncertainty widens, a new or unmapped path never selects an empty
set, and the generated packs carry references that resolve. The root's byte
ceiling is `test_claude_md_size.py`'s; the load budget is measured here
against the recorded post-reliability baseline — a target the tool enforces
(`inventory --check`), not a runtime observation.

Stdlib + pytest, plus `pack_render` for the rendered profiles. The tool under
test is `tools/ship_efficiency.py`, loaded by path because `tools/` is not a
package.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from dark_army_daemon import agent_models
from dark_army_menubar import pack_render

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "ship_efficiency", ROOT / "tools" / "ship_efficiency.py")
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)

FIXTURES = ROOT / "host" / "tests" / "fixtures" / "ship_efficiency"
ROLES = se.ROLES


@pytest.fixture(scope="module")
def context():
    return se.load_context_map(ROOT)


@pytest.fixture(scope="module")
def baseline_inventory():
    return json.loads((FIXTURES / "baseline-inventory.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def inventory():
    return se.inventory(ROOT)


# --- the map -----------------------------------------------------------------


def test_the_map_is_versioned_and_names_only_documents_that_exist(context):
    assert context["version"] == 1
    assert list(context["core"]) == ["CLAUDE.md", "AGENTS.md"]
    for name, ref in context["references"].items():
        path = ref["path"]
        assert not path.startswith(("/", "..")) and "/../" not in path, (name, path)
        assert (ROOT / path).is_file(), (name, path)
        assert (ROOT / path).read_text(encoding="utf-8").startswith("# "), name
        for extra in ref.get("with", []):
            assert (ROOT / extra).is_file(), (name, extra)
    for surface, names in context["surfaces"].items():
        assert names and set(names) <= set(context["references"]), surface
    for rule in context["cross_cutting"]:
        assert rule["why"] and rule["paths"]
        assert set(rule["add"]) <= set(context["references"])


def test_every_role_is_mapped_to_its_brief_and_shim(context):
    assert set(context["roles"]) == set(ROLES)
    for role, entry in context["roles"].items():
        assert entry["brief"] == f".claude/agents/{role}.md"
        assert entry["shim"] == f".codex/agents/{role}.toml"
        assert (ROOT / entry["brief"]).is_file() and (ROOT / entry["shim"]).is_file()
        assert entry["mode"] == ("plan" if role == "bc-planner" else "implement")


def test_no_reference_cycle_and_no_escape(context):
    """A reference names documents, never another reference; ``with`` entries
    must not point back at the map or at a document outside the checkout."""
    for name, ref in context["references"].items():
        assert ref["path"] != se.CONTEXT_MAP
        for extra in ref.get("with", []):
            assert extra != se.CONTEXT_MAP and extra != ref["path"], (name, extra)
            assert (ROOT / extra).resolve().is_relative_to(ROOT)


# --- selection ---------------------------------------------------------------


@pytest.mark.parametrize("surfaces,paths,expected", [
    (["host"], ["host/dark_army_daemon/session_stats.py"], {"host", "panel"}),
    (["host"], ["host/dark_army_daemon/samples.py"], {"host"}),
    (["tools"], ["tools/ship_efficiency.py"], {"development"}),
    (["panel"], ["panel/Sources/BobPanel/Inbox.swift"], {"panel"}),
    (["board"], ["host/dark_army_daemon/board.py"], {"board", "host", "panel"}),
    (["host", "panel"], ["host/dark_army_daemon/api_server.py",
                         "panel/Sources/BobPanel/DaemonClient.swift"], {"host", "panel"}),
])
def test_scoped_selection_is_the_union_with_cross_cutting_additions(context, surfaces, paths, expected):
    chosen, widened = se.select_references(context, surfaces, paths)
    assert chosen == expected
    assert widened == []


@pytest.mark.parametrize("surfaces,paths", [
    (["no-such-surface"], []),
    ([], ["somewhere/new/module.rs"]),
    ([], []),
    (["host"], ["host/dark_army_daemon/daemon.py", "brand-new-dir/x.py"]),
    ([""], [""]),
])
def test_uncertainty_selects_every_reference_and_says_why(context, surfaces, paths):
    chosen, widened = se.select_references(context, surfaces, paths)
    assert chosen == set(context["references"])
    assert widened, "the fallback must name what widened it"


def test_an_unmapped_path_never_selects_an_empty_set(context):
    chosen, _ = se.select_references(context, [], ["untracked/new-file.txt"])
    assert chosen and chosen == set(context["references"])


def test_a_map_that_names_a_missing_reference_is_refused():
    broken = {"references": {"a": {"path": "docs/a.md", "paths": ["a/"]}},
              "surfaces": {"s": ["ghost"]}, "cross_cutting": []}
    with pytest.raises(se.ShipEfficiencyError):
        se.select_references(broken, ["s"], ["a/x"])


# --- what every role loads ---------------------------------------------------


def test_every_role_loads_the_compact_core_and_its_brief(context, inventory):
    for name, scenario in inventory["scenarios"].items():
        for who, load in scenario["roles"].items():
            assert load["paths"][:2] == ["CLAUDE.md", "AGENTS.md"], (name, who)
            if who != "parent":
                assert f".claude/agents/{who}.md" in load["paths"], (name, who)
            assert len(load["paths"]) == len(set(load["paths"])), "a path counted twice"


def test_mode_change_loads_the_other_reference(context):
    for provider, files in context["workflow"].items():
        text = (ROOT / files["adapter"]).read_text(encoding="utf-8")
        plan = se.workflow_reference_set(files["adapter"], text, "plan")
        implement = se.workflow_reference_set(files["adapter"], text, "implement")
        assert plan == [files["common"], files["plan"]], provider
        assert implement == [files["common"], files["implement"]], provider
        assert "the other mode's reference" in text, provider
        for path in files.values():
            assert (ROOT / path).is_file(), path


def test_the_codex_shims_load_the_compact_root_the_brief_and_the_map():
    """Sandbox and reasoning unchanged; the load rule stated and no to-do
    file named (retired 22 Sep 2026). A model line, when present, is the
    Agent models setting's (`pack_install.pin_own_checkout`), never typed."""
    for role in ROLES:
        data = tomllib.loads((ROOT / ".codex/agents" / f"{role}.toml").read_text(encoding="utf-8"))
        text = data["developer_instructions"]
        assert "CLAUDE.md" in text and "AGENTS.md" in text and f".claude/agents/{role}.md" in text
        assert "docs/agent-context.json" in text
        assert "TODO" not in text
        assert "never from another role's selection" in text
        assert data["model_reasoning_effort"] == "high"
        assert data["sandbox_mode"] == (
            "workspace-write" if role in {"bc-planner", "bc-implementer"} else "read-only")
        if "model" in data:
            assert data["model"] in agent_models.allowed("codex", role.removeprefix("bc-"))


# --- preservation and budgets ------------------------------------------------


def test_the_preservation_map_resolves_and_every_old_paragraph_has_a_home():
    doc = (ROOT / se.REPORT_DOC).read_text(encoding="utf-8")
    pmap = se.preservation_map(doc)
    baseline = json.loads((FIXTURES / "claude-md-baseline.json").read_text(encoding="utf-8"))
    assert se.check_preservation(ROOT, pmap, baseline) == []
    # Seven since 22 Sep 2026: the mask's "two pictures" opening (3a47448626a1)
    # and the cast rebrand's "Cipher is chief of staff" (39c21aa5042d).
    # Seventeen later that day: the product-name sweep rewrote ten openings
    # that began with the old product or package name (f89ec2ae1817,
    # 59f446d87b63, b9c8624f1beb, 61e33fa6fa29, 12b0deee60fe, b21323c80b45,
    # 3aad295e9317, 3698506c2f36, 268ea9d4e385, 38c4fb148689).
    # Eighteen since 23 Sep 2026: the index re-named dark-army rewrote the
    # GitNexus resources table's opening, the old index name (b738f1813304).
    # Nineteen later that day: retiring the old install's migration code made
    # the build flags table's opening `--install` row false (09d9af449624).
    # Twenty later that day: the panel context's opening named the old settings folder's path inside its first seventy characters (c71aacfefc02).
    assert len(pmap["exceptions"]) <= 20, "every exception is a verified stale statement, named"
    for entry in pmap["exceptions"].values():
        assert entry["why"] and entry["replacement"]
    problems, edited = se.preservation_report(ROOT, pmap, baseline)
    assert problems == []
    # The edited-after-the-move notes are bounded by what the document itself
    # lists under "Edited after the move": a note for a paragraph the document
    # does not name is a relocation nobody has accounted for.
    listed = _edited_after_the_move(doc)
    assert listed, "the document lists the paragraphs known to be edited after the move"
    for note in edited:
        assert "was edited in docs/context-" in note or "was edited in CLAUDE.md in place" in note, note
        d = note.split()[2]
        assert d in listed, f"an edited relocation the document does not list: {note}"
    assert len(edited) <= len(listed)


def _edited_after_the_move(doc: str) -> set[str]:
    """The digests the report's *Edited after the move* table names."""
    match = re.search(r"^\*\*Edited after the move\.\*\*(.*?)(?=^\*\*|^## |\Z)", doc, re.M | re.S)
    assert match, "the preservation map has no 'Edited after the move' paragraph"
    return set(re.findall(r"^\| `([0-9a-f]{12})` \|", match.group(1), re.M))


def test_the_baseline_fixture_is_the_saved_pre_ship_copy_and_the_generator_reproduces_its_shape(tmp_path):
    """The map is checked against the file as the working tree held it when
    the ship began — the saved pre-ship copy — never against HEAD plus a
    patch reapplied by hand, which the verifier found does not reproduce it."""
    baseline = json.loads((FIXTURES / "claude-md-baseline.json").read_text(encoding="utf-8"))
    assert "saved pre-ship copy" in baseline["source"] and "authority" in baseline["source"]
    assert baseline["bytes"] == 140_021
    assert len(baseline["paragraphs"]) == 172
    assert len({e["digest"] for e in baseline["paragraphs"]}) == 172
    assert all(set(e) == {"digest", "section", "head", "words"} for e in baseline["paragraphs"])
    assert all(isinstance(e["words"], int) and e["words"] > 0 for e in baseline["paragraphs"])
    for sentence, digest_ in (("the phone's `PipelineView` stays", "173ed4d685d0"),
                              ("not a local process that reads the key file", "12b0deee60fe"),
                              ("399 of 2,038, 20 Sep 2026", "a55e92a6b4c9")):
        assert digest_ in {e["digest"] for e in baseline["paragraphs"]}, sentence
    text = "# Top\n\nfirst rule\n\n```bash\n# not a heading\ncmd\n```\n\n## Sub\n\nsecond rule\n\nfirst rule\n"
    made = se.baseline_paragraphs(text, "a test")
    assert made["source"] == "a test" and made["bytes"] == len(text.encode("utf-8"))
    assert [e["head"] for e in made["paragraphs"]] == ["first rule", "```bash # not a heading cmd ```", "second rule"]
    assert [e["words"] for e in made["paragraphs"]] == [2, 7, 2]
    assert [e["section"] for e in made["paragraphs"]] == ["# Top", "# Top", "## Sub"]
    assert made["paragraphs"][0]["digest"] == se.digest("first rule")
    out = tmp_path / "fixture.json"
    (tmp_path / "old.md").write_text(text, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(ROOT / "tools" / "ship_efficiency.py"), "baseline-paragraphs",
                           str(tmp_path / "old.md"), "--output", str(out), "--source", "a test"],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(out.read_text(encoding="utf-8")) == made


def test_the_relocated_paragraphs_are_verbatim_including_the_baseline_hunk():
    """The paragraph the pre-ship working tree carried (the `CardActionWeight`
    sentence) must have travelled whole into the panel document."""
    panel = (ROOT / "docs/context-panel.md").read_text(encoding="utf-8")
    assert "**A card outlines one verb, chosen by its column** (`CardActionWeight`)" in panel
    board = (ROOT / "docs/context-board.md").read_text(encoding="utf-8")
    assert "- **`dispatch.py`** — Dark Army as launcher" in board
    assert "Six properties hold it down" in board
    host = (ROOT / "docs/context-host.md").read_text(encoding="utf-8")
    assert "### Session State Model" in host
    assert "`_reap_permissions` substitutes" in host


def test_the_preservation_check_notices_a_dropped_paragraph(tmp_path):
    pmap = {"retained": [], "relocated": {"deadbeef0000": "docs/gone.md"},
            "exceptions": {}}
    baseline = {"paragraphs": [{"digest": "deadbeef0000", "head": "a rule"},
                               {"digest": "feedface0000", "head": "another"}]}
    (tmp_path / "CLAUDE.md").write_text("# root\n\nsomething else\n", encoding="utf-8")
    problems = se.check_preservation(tmp_path, pmap, baseline)
    assert any("destination missing" in p for p in problems)
    assert any("feedface0000" in p and "no mapped destination" in p for p in problems)


def test_a_relocated_paragraph_edited_in_its_new_home_is_noted_not_lost(tmp_path):
    """The subject documents are living contracts. A relocated paragraph that
    a later change edits in place is found by the opening the fixture
    recorded and listed as edited; one found by neither digest nor opening
    is lost, and a problem. The anchor needs the fixture's ``head``, and it
    is held against the fixture's ``words``: a block that keeps the opening
    and loses more than half the paragraph behind it is gutted, a problem,
    never a note."""
    (tmp_path / "CLAUDE.md").write_text("# root\n\nkept\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    original = ("**A long rule about the fleet, stated once, in full, for both clients.** "
                "It draws three bands in a 3.5-row viewport, absent rather than empty. "
                "The count on the header is the band's own, and a folded band still draws it.")
    assert "3.5-row" not in se.normalise(original)[:se.HEAD_CHARS], "the edit is past the recorded opening"
    edited = original.replace("3.5-row", "7.5-row")
    (tmp_path / "docs" / "sub.md").write_text(f"# sub\n\n{edited}\n", encoding="utf-8")
    d = se.digest(original)
    words = len(original.split())
    baseline = {"paragraphs": [{"digest": d, "section": "# s",
                                "head": se.normalise(original)[:se.HEAD_CHARS], "words": words}]}
    pmap = {"retained": [], "relocated": {d: "docs/sub.md"}, "exceptions": {}}
    problems, noted = se.preservation_report(tmp_path, pmap, baseline)
    assert problems == []
    assert noted == [f"relocated paragraph {d} ({se.normalise(original)[:40]!r}) was edited in docs/sub.md "
                     f"after the move ({words}→{words} words)"]
    assert se.check_preservation(tmp_path, pmap, baseline) == []
    # Grown in place: still a note, and the note says by how much.
    grown = edited + " A fourth sentence a later contract added."
    (tmp_path / "docs" / "sub.md").write_text(f"# sub\n\n{grown}\n", encoding="utf-8")
    problems, noted = se.preservation_report(tmp_path, pmap, baseline)
    assert problems == [] and noted[0].endswith(f"({words}→{len(grown.split())} words)")
    # Gutted behind its opening: the head still matches, the words do not — a problem, not a note.
    gutted = se.normalise(original)[:se.HEAD_CHARS].rsplit(" ", 1)[0] + "."
    assert se.normalise(gutted)[:se.HEAD_CHARS] != se.normalise(original)[:se.HEAD_CHARS]
    gutted = se.normalise(original)[:se.HEAD_CHARS] + "* Nothing else."
    assert se.normalise(gutted)[:se.HEAD_CHARS] == se.normalise(original)[:se.HEAD_CHARS]
    assert len(gutted.split()) < words * se.GUTTED_FRACTION
    (tmp_path / "docs" / "sub.md").write_text(f"# sub\n\n{gutted}\n", encoding="utf-8")
    problems, noted = se.preservation_report(tmp_path, pmap, baseline)
    assert noted == []
    assert problems == [f"relocated paragraph {d} ({se.normalise(original)[:40]!r}) in docs/sub.md keeps its "
                        f"opening but {words}→{len(gutted.split())} words: gutted, not edited in place"]
    # Reversed behind its opening reads the same way: the anchor alone proves nothing.
    reversed_ = se.normalise(original)[:se.HEAD_CHARS] + "* " + " ".join(reversed(original.split()[13:20]))
    assert len(reversed_.split()) < words * se.GUTTED_FRACTION
    (tmp_path / "docs" / "sub.md").write_text(f"# sub\n\n{reversed_}\n", encoding="utf-8")
    assert len(se.check_preservation(tmp_path, pmap, baseline)) == 1
    # A fixture with no recorded length cannot judge, so the note says what is there now.
    (tmp_path / "docs" / "sub.md").write_text(f"# sub\n\n{gutted}\n", encoding="utf-8")
    wordless = {"paragraphs": [{"digest": d, "section": "# s", "head": se.normalise(original)[:se.HEAD_CHARS]}]}
    problems, noted = se.preservation_report(tmp_path, pmap, wordless)
    assert problems == [] and noted[0].endswith(f"({len(gutted.split())} words now)")
    (tmp_path / "docs" / "sub.md").write_text(f"# sub\n\n{edited}\n", encoding="utf-8")
    # Verbatim: neither a problem nor a note.
    (tmp_path / "docs" / "sub.md").write_text(f"# sub\n\n{original}\n", encoding="utf-8")
    assert se.preservation_report(tmp_path, pmap, baseline) == ([], [])
    # Gone, or rewritten from its first words: lost.
    (tmp_path / "docs" / "sub.md").write_text("# sub\n\nSomething else entirely about the fleet.\n", encoding="utf-8")
    problems, noted = se.preservation_report(tmp_path, pmap, baseline)
    assert problems == [f"relocated paragraph {d} is not in docs/sub.md"] and noted == []
    # An edit inside the recorded opening reads as lost: the anchor is conservative.
    (tmp_path / "docs" / "sub.md").write_text(f"# sub\n\n{original.replace('once', 'twice')}\n", encoding="utf-8")
    assert se.check_preservation(tmp_path, pmap, baseline) == [f"relocated paragraph {d} is not in docs/sub.md"]
    # No recorded opening means no anchor: only the digest counts.
    (tmp_path / "docs" / "sub.md").write_text(f"# sub\n\n{edited}\n", encoding="utf-8")
    headless = {"paragraphs": [{"digest": d, "section": "# s"}]}
    assert se.check_preservation(tmp_path, pmap, headless) == [f"relocated paragraph {d} is not in docs/sub.md"]


def test_inventory_check_prints_an_edited_relocation_as_a_note(tmp_path):
    inventory = se.inventory(ROOT)
    baseline_inv = json.loads((FIXTURES / "baseline-inventory.json").read_text(encoding="utf-8"))
    notes: list[str] = []
    assert se.check_inventory(ROOT, inventory, baseline_inv, notes) == []
    proc = subprocess.run([sys.executable, str(ROOT / "tools" / "ship_efficiency.py"), "inventory",
                           "--root", str(ROOT), "--check"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for note in notes:
        assert f"note: {note}" in proc.stdout
    assert proc.stdout.count("note: ") == len(notes)


def _escaping_root(tmp_path):
    """A checkout-shaped folder beside a file outside it, with a symlink
    from inside pointing at that file."""
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "x.md"
    outside.write_text("# outside\n\nsmuggled rule\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("# root\n\nkept\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "inside.md").write_text("# inside\n\nsmuggled rule\n", encoding="utf-8")
    (root / "docs" / "link.md").symlink_to(outside)
    return root, outside


def test_containment_resolves_dot_dot_and_symlinks(tmp_path):
    root, outside = _escaping_root(tmp_path)
    assert se.inside_checkout(root, "docs/inside.md")
    assert not se.inside_checkout(root, "docs/../../x.md")
    assert not se.inside_checkout(root, "../x.md")
    assert not se.inside_checkout(root, "docs/link.md"), "a symlink out of the tree is followed before the test"
    assert not se.inside_checkout(root, str(outside))
    assert not se.inside_checkout(root, "")
    assert not se.inside_checkout(root, ".")
    assert se.inside_checkout(root, str(root / "docs" / "inside.md")), "an absolute path under the root is inside"


def test_a_relocation_destination_may_not_leave_the_checkout(tmp_path):
    """``docs/../../x.md`` and a symlink out of the tree both used to pass the
    prefix test and be read; both are refused before any read."""
    root, _ = _escaping_root(tmp_path)
    d = se.digest("smuggled rule")
    baseline = {"paragraphs": [{"digest": d, "head": "smuggled rule"}]}
    assert se.check_preservation(root, {"retained": [], "relocated": {d: "docs/inside.md"}, "exceptions": {}}, baseline) == []
    for dest in ("docs/../../x.md", "docs/link.md", "../x.md"):
        problems = se.check_preservation(root, {"retained": [], "relocated": {d: dest}, "exceptions": {}}, baseline)
        assert any(p == f"relocated {d} escapes the checkout: {dest}" for p in problems), (dest, problems)
        assert not any("is not in" in p for p in problems), "refused before the read, not read then compared"


def test_a_reference_path_or_with_entry_may_not_leave_the_checkout(tmp_path):
    root, _ = _escaping_root(tmp_path)
    for folder, refs in ((".claude", "`references/common.md` `references/plan.md` `references/implement.md`"),
                         (".agents", "`.claude/skills/ship/references/common.md` "
                                     "`.claude/skills/ship/references/plan.md` "
                                     "`.claude/skills/ship/references/implement.md`")):
        (root / folder / "skills" / "ship").mkdir(parents=True)
        (root / folder / "skills" / "ship" / "SKILL.md").write_text(refs, encoding="utf-8")
    (root / ".claude" / "skills" / "ship" / "references").mkdir()
    for name in ("common", "plan", "implement"):
        (root / ".claude" / "skills" / "ship" / "references" / f"{name}.md").write_text("# r\n", encoding="utf-8")
    context = {"version": 1, "core": ["CLAUDE.md"], "references": {
        "a": {"path": "docs/../../x.md", "with": [], "paths": ["a/"]},
        "b": {"path": "docs/link.md", "with": ["docs/../../x.md"], "paths": ["b/"]},
        "c": {"path": "docs/inside.md", "with": ["docs/link.md", "docs/inside.md"], "paths": ["c/"]},
    }, "surfaces": {}, "cross_cutting": []}
    (root / "docs" / "agent-context.json").write_text(json.dumps(context), encoding="utf-8")
    problems = se.check_inventory(root, {"documents": {}, "aggregate_bytes": 1}, None)
    assert "reference a escapes the checkout: docs/../../x.md" in problems
    assert "reference b escapes the checkout: docs/link.md" in problems
    assert "reference b escapes the checkout: docs/../../x.md" in problems
    assert "reference c escapes the checkout: docs/link.md" in problems
    assert not any(p.startswith("reference c") and "docs/inside.md" in p for p in problems)
    assert not any("missing" in p and "x.md" in p for p in problems), "refused, never read"


def test_the_compact_root_is_under_its_ceiling(inventory):
    assert inventory["documents"]["CLAUDE.md"]["bytes"] <= se.ROOT_CEILING


# The floor is `se.LOAD_REDUCTION` (0.28 since 21 Sep 2026; `docs/ship-efficiency.md`).
def test_scenario_loads_shrink_by_at_least_the_floor(inventory, baseline_inventory):
    before = baseline_inventory["aggregate_bytes"]
    after = inventory["aggregate_bytes"]
    assert baseline_inventory["rule"] == "legacy" and inventory["rule"] == "mapped"
    assert after <= before * (1 - se.LOAD_REDUCTION), (before, after)
    for name in ("plan-only", "python-only", "cross-surface"):
        assert inventory["scenarios"][name]["bytes"] < baseline_inventory["scenarios"][name]["bytes"], name


def test_the_baseline_fixture_records_the_legacy_rule_and_no_home_path(baseline_inventory):
    assert baseline_inventory["schema"] == se.SCHEMA
    blob = (FIXTURES / "baseline-inventory.json").read_text(encoding="utf-8")
    assert "/Users/" not in blob and "/home/" not in blob and "/private/tmp" not in blob
    for role in ROLES:
        assert baseline_inventory["roles"][role]["model_reasoning_effort"] == "high"
        assert baseline_inventory["roles"][role].get("model") is None


def test_role_settings_are_unchanged_from_the_baseline(inventory, baseline_inventory):
    for role in ROLES:
        before = baseline_inventory["roles"][role]
        after = inventory["roles"][role]
        # `model` is not a hand-kept setting any more: the Agent models
        # setting owns it and pins it per machine (`pin_own_checkout`).
        for key in ("sandbox_mode", "model_reasoning_effort", "tools"):
            assert before.get(key) == after.get(key), (role, key)


def test_inventory_check_passes_on_this_tree(inventory, baseline_inventory):
    assert se.check_inventory(ROOT, inventory, baseline_inventory) == []


def test_inventory_check_names_a_missing_baseline_and_an_oversized_root(inventory, baseline_inventory):
    fat = json.loads(json.dumps(inventory))
    fat["documents"]["CLAUDE.md"]["bytes"] = se.ROOT_CEILING + 1
    problems = se.check_inventory(ROOT, fat, None)
    assert any("compact ceiling" in p for p in problems)
    assert any("no recorded baseline" in p for p in problems)
    small = json.loads(json.dumps(baseline_inventory))
    small["aggregate_bytes"] = inventory["aggregate_bytes"]
    assert any(f"not {int(se.LOAD_REDUCTION * 100)}% smaller" in p for p in se.check_inventory(ROOT, inventory, small))


# --- the rendered packs --------------------------------------------------------


@pytest.mark.parametrize("profile", pack_render.PROFILES)
def test_generated_profile_references_resolve_and_are_mirrored(profile):
    mapping = pack_render.render(profile, "xx", "fixture-project")
    adapter = ".claude/skills/ship/SKILL.md"
    text = mapping[adapter].decode("utf-8")
    for mode in ("plan", "implement"):
        refs = se.workflow_reference_set(adapter, text, mode)
        assert refs == [".claude/skills/ship/references/common.md",
                        f".claude/skills/ship/references/{mode}.md"], (profile, mode)
        for ref in refs:
            assert ref in mapping, (profile, ref)
            mirrored = ref.replace(".claude/", ".agents/", 1)
            assert mapping[mirrored] == mapping[ref], (profile, ref)
    resolved = se.resolve_rendered_workflow(mapping, ".agents/skills/ship/SKILL.md")
    assert "{{" not in resolved
    for phrase in ("Phase 6f: blind verify", "Phase 5b: close out", "Phase 0: preconditions",
                   "## The handoff packet", "One execution table per verifier pass"):
        assert phrase in resolved, (profile, phrase)


def test_a_rendered_adapter_that_names_a_missing_reference_is_refused():
    mapping = {".agents/skills/ship/SKILL.md": b"read `references/common.md` then `references/plan.md`"}
    with pytest.raises(se.ShipEfficiencyError):
        se.resolve_rendered_workflow(mapping, ".agents/skills/ship/SKILL.md", "plan")


def test_the_template_references_live_under_an_installer_destination():
    from dark_army_menubar import pack_install
    for name in ("common", "plan", "implement"):
        for folder in (".claude", ".agents"):
            assert pack_install._admissible(f"{folder}/skills/ship/references/{name}.md")


def test_the_template_keeps_its_profile_guards_and_stage_limits():
    template = ROOT / "host/dark_army_menubar/agent_pack/template/.claude/skills/ship"
    implement = (template / "references/implement.md").read_text(encoding="utf-8")
    assert "{{P}}-verifier" in implement and "{{P}}-bug-auditor" in implement
    assert "Max\n2 verify cycles" in implement or "Max 2 verify cycles" in " ".join(implement.split())
    assert "Max 3 iterations" in " ".join(implement.split())
    assert "six implementer dispatches" in implement
    assert "Reviewers** table" in implement or "Reviewers table" in implement
    assert "In scope" in implement and "Follow-ups not filed" in implement
    common = (template / "references/common.md").read_text(encoding="utf-8")
    assert "## Ground truth" in common and "docs/context.md" in common
    assert "filed_followups" in common and "in-scope findings only" in common
    assert re.search(r"^## Phase 0: preconditions$", common, re.M)
