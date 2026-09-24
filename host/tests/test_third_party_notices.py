"""The app credits what it is built on, and carries the credits inside it.

`THIRD_PARTY_NOTICES.md` at the repository root names every outside
component the installed app ships, with its licence, and nothing else: Dark
Army's own copyright is `LICENSE`'s one line. A dependency added to `host/requirements.txt` or the panel's
`Package.resolved` without a line there fails here, as does anything in the
frozen-bundle manifest (`host/tests/data/frozen-components.txt`, written by
`tools/frozen_components.py` from a built app) without its entry; and
`build.sh` must copy the page and `LICENSE` into the bundle before anything is
signed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NOTICES = REPO / "THIRD_PARTY_NOTICES.md"
REQUIREMENTS = REPO / "host" / "requirements.txt"
RESOLVED = REPO / "panel" / "Package.resolved"
BUILD_SH = REPO / "host" / "build.sh"
MANIFEST = REPO / "host" / "tests" / "data" / "frozen-components.txt"
LICENSE = REPO / "LICENSE"
GENERATOR = REPO / "tools" / "frozen_components.py"

#: SHA-256 of the two words of the name of the author of the project this
#: one began as a fork of. Held as digests so that the name itself appears
#: nowhere in the public tree, a test included.
FORMER_AUTHOR_DIGESTS = frozenset({
    "52667e8b16cdc0747e5c2b6c57328cb2fd11e4fa8b9fd5ae94be6e3d1c71fcc1",
    "b56e00f25d64e42401e411fd9b5a25787a40fb68b57927929a1027e2f25a2cc4",
})


def _notices() -> str:
    return NOTICES.read_text(encoding="utf-8").lower()


def _requirement_names() -> list[str]:
    names = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name = re.split(r"[\s<>=!~;\[]", line, maxsplit=1)[0]
        if name:
            names.append(name)
    return names


def test_every_runtime_requirement_is_credited():
    names = _requirement_names()
    assert names, "requirements.txt parsed to nothing"
    text = _notices()
    missing = [name for name in names if name.lower() not in text]
    assert missing == []


def test_every_resolved_swift_package_is_credited():
    pins = json.loads(RESOLVED.read_text(encoding="utf-8"))["pins"]
    assert pins
    text = _notices()
    missing = [pin["identity"] for pin in pins
               if pin["identity"].lower() not in text]
    assert missing == []


def test_the_frozen_extras_are_credited():
    text = _notices()
    for name in ("pyobjc", "cffi",
                 "python software foundation license"):
        assert name in text, name
    # The interpreter's own entry, not the section heading that names Python.
    assert re.search(r"^### Python 3\.\d+", NOTICES.read_text(encoding="utf-8"),
                     re.M)


def test_the_page_credits_no_upstream_project():
    raw = NOTICES.read_text(encoding="utf-8")
    assert not re.search(r"^## Upstream\b", raw, re.M)
    text = raw.lower()
    assert "clawd" not in text
    assert "forked" not in text
    words = set(re.findall(r"[a-z]+", text))
    assert not {w for w in words
                if hashlib.sha256(w.encode()).hexdigest() in FORMER_AUTHOR_DIGESTS}


def test_the_page_starts_with_the_generators_own_header():
    spec = importlib.util.spec_from_file_location("frozen_components", GENERATOR)
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    assert NOTICES.read_text(encoding="utf-8").startswith("\n".join(generator.HEADER))


def test_the_licence_carries_one_copyright_line_and_it_is_ours():
    lines = LICENSE.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "MIT License"
    assert [line for line in lines if line.startswith("Copyright")] == [
        "Copyright (c) 2026 Robert Androsz"]


def _manifest() -> list[list[str]]:
    rows = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            rows.append(line.split("\t"))
    return rows


def test_the_manifest_lists_what_a_frozen_bundle_carries():
    """The manifest is read off a built app by `tools/frozen_components.py
    manifest` (refresh it after a dependency change). It must at least hold
    every direct requirement, or it was not read off this tree's build."""
    rows = _manifest()
    kinds = {row[0] for row in rows}
    assert {"python", "native", "runtime", "bootstrap"} <= kinds
    frozen = {row[1].lower() for row in rows if row[0] == "python"}
    missing = [name for name in _requirement_names() if name.lower() not in frozen]
    assert missing == []


def test_every_frozen_component_is_credited():
    """A component py2app freezes in without a notice fails here, once the
    manifest is refreshed from the build that carries it."""
    headings = [line for line in NOTICES.read_text(encoding="utf-8").splitlines()
                if line.startswith("### ")]
    missing = []
    for row in _manifest():
        kind, name = row[0], row[1]
        if kind in ("python", "runtime"):
            want = f"### {name} {row[2]}"
            ok = any(h == want or h.startswith(want + " ") for h in headings)
        elif kind == "native":
            ok = any(name in h for h in headings)
        else:
            ok = any(h.startswith(f"### {name} bootstrap") for h in headings)
        if not ok:
            missing.append("\t".join(row))
    assert missing == []


def _copy_line(text: str, name: str) -> int:
    pattern = re.compile(
        r'^cp "\$SCRIPT_DIR/\.\./' + re.escape(name)
        + r'" "\$APP/Contents/Resources/"$', re.M)
    match = pattern.search(text)
    assert match, f"build.sh does not copy {name} into Contents/Resources"
    return match.start()


def test_build_copies_both_files_before_the_manifest_and_the_signing():
    text = BUILD_SH.read_text(encoding="utf-8")
    first_codesign = re.search(r"^\s*codesign ", text, re.M)
    assert first_codesign
    manifest = text.index("release-manifest.json")
    for name in ("THIRD_PARTY_NOTICES.md", "LICENSE"):
        at = _copy_line(text, name)
        assert at < first_codesign.start(), name
        assert at < manifest, name
