"""`docs/2026-09-06-supported-versions.md` must keep agreeing with the tree.

The document is the supported-versions contract: which components are matched
by construction, which two may lag by one release, and where the floors sit
today. Every number it states is re-derivable from a symbol, so this module
reads the document as its *input* and asks the tree the same question:

- the extension floor is one release below `vscode-extension/package.json`,
  and baseline `*_MIN_VERSION` gates sit at or below it; native reply is an
  optional capability pinned to the current package;
- every gate constant is named in the document, so a gate added later and
  never written down fails here;
- the phone declares exactly one version and the document names it;
- every repository path the document cites still opens;
- both inventory tables have the shape the policy needs (a removable row
  cannot be filed without a follow-on card);
- the protected markers are live symbols in daemon source.

Shape follows ``test_docs_current.py`` (same ``REPO_ROOT`` derivation, same
raw-text ``PATH_RE``) without importing from it: a test that imports another
test module makes both undeletable. The only product modules imported are
``dark_army_menubar.build_check`` (stdlib only, never imported by the
runtime) and ``dark_army_daemon.vscode_reveal`` (import-clean; its
constants are read without starting a daemon).
"""

import json
import os
import re
from pathlib import Path

import pytest

from dark_army_daemon import vscode_reveal
from dark_army_menubar import build_check

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = REPO_ROOT / "docs" / "2026-09-06-supported-versions.md"
MANIFEST = REPO_ROOT / "vscode-extension" / "package.json"
REVEAL_SOURCE = REPO_ROOT / "host" / "dark_army_daemon" / "vscode_reveal.py"
DAEMON_SOURCE_DIR = REPO_ROOT / "host" / "dark_army_daemon"

# Scan the *raw* text rather than backtick pairs — see test_docs_current.py's
# note on fenced code re-pairing the backticks (measured: 2 of 11 found). A
# `path.py:123` citation still matches up to the extension.
PATH_RE = re.compile(
    r"(?<![\w/~.-])"
    r"((?:host|panel|tools|ios|relay|vscode-extension|assets|plans)/"
    r"[\w./-]*\.(?:py|swift|sh|json|md|toml))"
)

GATE_RE = re.compile(r"^([A-Z_]+_MIN_VERSION) = \((\d+), (\d+), (\d+)\)", re.MULTILINE)

#: The capability markers the document's protected table names as the phone
#: half of the policy. Each must be in the document *and* in daemon source.
PROTECTED_MARKERS = (
    "outcomes_supported",
    "queue_writable",
    "preferences_writable",
    "home_sealed",
    "dispatch_enabled",
    "prepare_enabled",
    "knowledge_supported",
)

REMOVABLE_HEADING = "## What the policy makes removable"
FLOORS_HEADING = "## Floors today"


def _document() -> str:
    assert DOCUMENT.is_file(), f"{DOCUMENT} is missing"
    return DOCUMENT.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body under ``heading`` up to the next ``## `` line."""
    start = text.find(f"\n{heading}\n")
    assert start >= 0, f"document has no {heading!r} section"
    body = text[start + len(heading) + 2:]
    nxt = re.search(r"^## ", body, re.MULTILINE)
    return body[: nxt.start()] if nxt else body


def _table_rows(section: str) -> list:
    """Every `| a | b |` data row as a list of stripped cells, skipping the
    header and the `|---|` separator."""
    rows = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        rows.append(cells)
    return rows[1:]  # drop the header


def _gates_in_source() -> dict:
    """`{name: (major, minor, patch)}` for every gate assignment in
    vscode_reveal.py, cross-checked against the imported module so a regex
    that stops matching cannot pass quietly."""
    found = {m.group(1): tuple(int(g) for g in m.groups()[1:])
             for m in GATE_RE.finditer(REVEAL_SOURCE.read_text(encoding="utf-8"))}
    assert found, "no *_MIN_VERSION assignments found in vscode_reveal.py"
    for name, value in found.items():
        assert getattr(vscode_reveal, name) == value, f"{name} regex disagrees with the module"
    return found


def _floor_from_manifest(manifest_path) -> str:
    """One patch below the version the manifest declares. The "minus one
    patch" form is the convenience the document says holds while releases are
    consecutive patches."""
    verdict = build_check.package_version(manifest_path)
    assert verdict.ok, verdict.reason
    major, minor, patch = (int(p) for p in verdict.reason.split("."))
    assert patch >= 1, f"cannot take one patch below {verdict.reason}"
    return f"{major}.{minor}.{patch - 1}"


def _floor_cell(document_text: str, component: str) -> str:
    for cells in _table_rows(_section(document_text, FLOORS_HEADING)):
        if cells and cells[0] == component:
            assert len(cells) >= 3, f"floor row for {component!r} is short"
            return cells[1]
    raise AssertionError(f"no floor row for {component!r}")


#: Capability gates a supported window may sit below, each named in the
#: document with the reason. `NATIVE_REPLY_MIN_VERSION` is pinned to the
#: current extension; `SUBFOLDER_SPAWN_MIN_VERSION` is the release a card's
#: terminal can first open in the card's own worktree
#: (`docs/card-worktrees.md`) — an older window is refused in words that
#: name the reload and the per-project isolation switch, never started in
#: the main checkout. It may sit above the floor, never above the current
#: package; once the floor passes it, it is an ordinary baseline gate.
OPTIONAL_GATES = ("NATIVE_REPLY_MIN_VERSION", "SUBFOLDER_SPAWN_MIN_VERSION")


def _optional_gates_hold(gates: dict, current: tuple) -> None:
    for name in OPTIONAL_GATES:
        assert name in gates, f"{name} is no longer declared in vscode_reveal.py"
        assert gates[name] <= current, (
            f"{name} = {gates[name]} asks for an extension newer than the "
            f"current package {current}")


def check_extension_floor(manifest_path, document_text: str) -> str:
    """The seam the negative case drives: derive the floor from *this*
    manifest and hold *this* document to it. Returns the derived floor."""
    expected = _floor_from_manifest(manifest_path)
    cell = _floor_cell(document_text, "VS Code extension")
    assert f"`{expected}`" in cell, (
        f"the document's extension floor cell reads {cell!r}, but the manifest "
        f"derives {expected}; if the last published extension was not "
        f"{expected}, edit the document first"
    )
    expected_tuple = tuple(int(p) for p in expected.split("."))
    gates = _gates_in_source()
    current = tuple(int(p) for p in build_check.package_version(manifest_path).reason.split("."))
    assert gates["NATIVE_REPLY_MIN_VERSION"] == current, (
        "optional native reply must require exactly the current extension"
    )
    _optional_gates_hold(gates, current)
    baseline = {name: value for name, value in gates.items()
                if name not in OPTIONAL_GATES}
    highest = max(baseline.items(), key=lambda kv: kv[1])
    assert highest[1] <= expected_tuple, (
        f"{highest[0]} = {highest[1]} sits above the floor {expected}: a "
        "supported install would be refused a control"
    )
    return expected


# --- the cases --------------------------------------------------------------


def test_the_extension_floor_is_one_release_below_the_current_package():
    """Fails the day `package.json` bumps without the document moving."""
    check_extension_floor(MANIFEST, _document())


def test_a_bumped_manifest_fails_the_floor_check(tmp_path):
    """The negative case: pretend the extension moved ahead. The document
    still names today's floor, so the check must refuse rather than pass."""
    manifest = tmp_path / "package.json"
    manifest.write_text(json.dumps({"name": "dark-army-ide", "version": "0.1.23"}),
                        encoding="utf-8")
    with pytest.raises(AssertionError, match="edit the document first"):
        check_extension_floor(manifest, _document())


def test_every_extension_gate_is_named_in_the_document():
    text = _document()
    missing = [name for name in _gates_in_source() if name not in text]
    assert not missing, f"gates in vscode_reveal.py the document never names: {missing}"


def test_no_extension_gate_sits_above_the_floor():
    floor = tuple(int(p) for p in _floor_from_manifest(MANIFEST).split("."))
    gates = _gates_in_source()
    current = tuple(int(p) for p in build_check.package_version(MANIFEST).reason.split("."))
    assert gates["NATIVE_REPLY_MIN_VERSION"] == current
    _optional_gates_hold(gates, current)
    above = {name: value for name, value in gates.items()
             if name not in OPTIONAL_GATES and value > floor}
    assert not above, f"gates above the floor {floor}: {above}"


def test_the_phone_declares_exactly_one_version_and_the_document_names_it():
    versions = build_check.ios_marketing_versions(REPO_ROOT)
    assert len(versions) == 1, f"the phone declares {versions}; release_gate refuses this too"
    assert f"`{versions[0]}`" in _floor_cell(_document(), "Phone")


def test_the_document_cites_only_paths_that_exist():
    cited = PATH_RE.findall(_document())
    assert len(cited) >= 10, f"only {len(cited)} citations found; the regex is not matching"
    # `plans/` is git-ignored, so a fresh clone (CI) checks every other path —
    # even when a branch force-adds one plan and so creates the folder.
    plans_kept = (REPO_ROOT / "plans").is_dir() and not os.environ.get("CI")
    missing = sorted({c for c in cited if not (REPO_ROOT / c).exists()
                      and (plans_kept or not c.startswith("plans/"))})
    assert not missing, f"cited paths that do not exist: {missing}"


def test_the_protected_rows_name_live_symbols():
    text = _document()
    sources = "\n".join(p.read_text(encoding="utf-8")
                        for p in sorted(DAEMON_SOURCE_DIR.glob("*.py")))
    problems = []
    for marker in PROTECTED_MARKERS:
        if marker not in text:
            problems.append(f"{marker} is not in the document")
        if f'"{marker}"' not in sources:
            problems.append(f"{marker} is not a key written in host/dark_army_daemon/")
    assert not problems, "\n".join(problems)


def test_the_removable_table_is_fully_filled_in():
    """A row cannot be filed without a follow-on card."""
    rows = _table_rows(_section(_document(), REMOVABLE_HEADING))
    assert len(rows) >= 4, f"only {len(rows)} removable rows parsed"
    for cells in rows:
        assert len(cells) == 4, f"removable row has {len(cells)} cells: {cells[0]!r}"
        assert all(cells), f"removable row has an empty cell: {cells[0]!r}"


def test_the_matched_pair_has_one_source_of_truth():
    versions = build_check.release_versions(REPO_ROOT)
    for key in ("app", "panel_embedded", "extension", "phone"):
        assert key in versions, f"release_versions() lacks {key!r}"
    assert "release_versions" in _document()
