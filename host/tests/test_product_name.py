"""Dark Army in every word and every folder; `bob` only on the closed list.

Since 22 Sep 2026 the product is called Dark Army in every word a person or a
model reads — labels, docs, comments, docstrings, briefs, tool descriptions,
hints, log lines — and in every folder and file of the checkout and of an
install (`host/dark_army_daemon`, `host/dark_army_menubar`, `~/.dark-army`,
the `dark-army-*` helpers). The old name survives only where moving it would
break an installed machine, and that list is closed (`CLAUDE.md`, the product
name paragraph):

* wire and on-disk identifiers — the MCP server's legacy name `bob`, its
  `bob_*` tools and `<channel source="bob">`, a **window-only alias** live
  beside `dark-army` / `dark_army_*` for the dual-name window and removed by
  the follow-up *End the channel dual-name window*; the `X-Bob-*` headers,
  the `bob-tldr` /
  `bob-actions` markers, the bundle identifiers, the Swift targets, the
  legacy key folder `.bob-companion/key` (read behind `.dark-army/key` while
  the read window is open), the `BOB_*` environment variables — are
  asserted **positively**, token by token (`PROTECTED`), so a session
  "finishing the rename" cannot move one of them silently;
* the words "Bob Companion" name the retired install, and only in the few
  files that still have a reason to (`LEGACY_MENTION_FILES`, each with a
  ceiling so the pile cannot regrow);
* the old package names `bob_companion_daemon` / `bob_companion_menubar`
  survive only in the broker contract's dated incident line
  (`LEGACY_MODULE_FILES`);
* the hyphenated `bob-companion` only in the shapes `_HYPHENATED_OK` names;
* no tracked file's name begins `bob-`; the old mascot's screen captures were
  the last and are deleted.

Everything else — the bare word, in prose, comments, docstrings, briefs and
model-facing text alike — is gone, and each check below is proved to fire on
a doctored copy of a real file.

History is out of scope: `plans/`, the dated documents under `docs/` and
`docs/research/`, and recorded evidence (the preservation baseline under
`host/tests/fixtures/ship_efficiency/`, the terminal captures under
`host/tests/data/permission_hold_livefire/`) say what was true on their date.
The scans read tracked files (`git ls-files`), so a build folder or another
session's uncommitted file is not judged here.
"""

from __future__ import annotations

import ast
import functools
import json
import plistlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SELF = "host/tests/test_product_name.py"

# --- the tree ---------------------------------------------------------------
#: What is history or recorded evidence, never rewritten by a rename.
_HISTORY = re.compile(
    r"^(?:plans/|docs/research/|docs/20\d\d-"
    r"|host/tests/fixtures/ship_efficiency/"
    r"|host/tests/data/permission_hold_livefire/"
    r"|claude-md-baseline\.json$)")


@functools.lru_cache(maxsize=None)
def _tracked() -> tuple[str, ...]:
    """Every tracked path, relative to the checkout."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True,
                         check=True).stdout
    return tuple(p for p in out.decode("utf-8").split("\0") if p)


@functools.lru_cache(maxsize=None)
def _text(rel: str) -> str | None:
    """The file's text, or None for a binary or unreadable file."""
    try:
        return (REPO / rel).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _current_text_files():
    """(rel, text) for every tracked text file that is not history."""
    for rel in _tracked():
        if _HISTORY.match(rel):
            continue
        text = _text(rel)
        if text is not None:
            yield rel, text


# --- (a) the identifier boundary --------------------------------------------
#: Every one of these must still be spelled this way somewhere in the tree.
PROTECTED = (
    # The legacy per-project key folder, read behind `.dark-army/key` while
    # the read window is open, and `~/.bob-companion`, the old state folder's
    # name that stays as a permanent link on every migrated machine.
    ".bob-companion",
    # Where the app lives now.
    ".dark-army",
    "dark-army-notify",
    "dark-army-statusline",
    "dark-army-channel",
    "dark-army-close-out",
    "dark-army-shunt",
    "dark-army-ide",
    "com.dark-army.menubar",
    # Environment variables: inherited by sessions in flight and set in
    # people's shells.
    "BOB_COMPANION_PORT",
    "CLAWD_TANK_PORT",
    # The private hook socket's address, read by every hook client.
    "DARK_ARMY_HOOK_SOCKET",
    # The board MCP server and its tools under the current name.
    "dark_army_add_card",
    "dark_army_attach_plan",
    "dark_army_attach_report",
    "dark_army_close_card",
    "dark_army_needs_manual_check",
    "dark_army_answer_card",
    "dark_army_knowledge_read",
    "dark_army_knowledge_write",
    'source="dark-army"',
    "server:dark-army",
    "dark-army-board",
    "darkArmy.showThisSession",
    # The dual-name window: the legacy server name and its tools, still
    # registered beside `dark-army` so a session born under `bob` keeps its
    # tools. Removed by the follow-up *End the channel dual-name window*.
    "bob_add_card",
    "bob_attach_plan",
    "bob_attach_report",
    "bob_close_card",
    "bob_needs_manual_check",
    "bob_answer_card",
    "bob_knowledge_read",
    "bob_knowledge_write",
    'source="bob"',
    "server:bob",
    # Turn markers parsed out of every running session's messages.
    "bob-tldr",
    "bob-actions",
    # Headers the panel, the phone and the extension speak.
    "X-Bob-Token",
    "X-Bob-Frame",
    "X-Bob-Channel",
    "x-bob-companion-authorization",
    # The bundle identifiers: notification, automation and LaunchServices
    # records are keyed by them, so they did not move with the bundle.
    "com.bob-companion.menubar",
    "com.bob-companion.panel",
    "com.robertandrosz.bobphone",
    # Codex's MCP registration name before the rename: migration code now,
    # removed by the installer where it owns the entry.
    "bob-companion-board",
    "BobFleetWidget",
    "BobFleetTile",
    # The command's hidden alias for the dual-name window: registered, not
    # contributed, so a person's keybinding naming it still resolves.
    "bobCompanion.showThisSession",
    "BobPanel",
    "BobPhone",
    "terminal_title = [] # managed by Bob Companion",
)


@functools.lru_cache(maxsize=None)
def _tree_blob() -> str:
    return "\n".join(text for rel, text in _current_text_files() if rel != SELF)


@pytest.mark.parametrize("token", PROTECTED)
def test_identifiers_unchanged(token: str) -> None:
    """Nothing on disk and nothing on the wire moved."""
    assert token in _tree_blob(), \
        f"LOST identifier: {token!r} no longer appears anywhere in the tree"


# --- (b) the bare word is gone everywhere -----------------------------------
#: `Bob` as a bare word: not glued to a larger identifier on either side, and
#: not the first half of the retired product's name, which (c) governs.
#: `BobPanel`, `X-Bob-Token`, `BobCompanion`, `local.bob.BobPhone` are
#: identifiers and pass; `Bob's`, `Bob cannot`, `Bob-owned` are prose.
_BARE_BOB = re.compile(r"(?<![A-Za-z0-9_.\-])Bob(?![A-Za-z0-9_])(?!\s+Companion)")
#: The lowercase form. `-`, `_` and `.` glue on either side (`bob-card`,
#: `bob.panelOpenBoard`, `.bob-companion`, `bob_add_card`); `@` and `:` do
#: not, so a prompt line's `root@bob:` is a bare word and fails.
_BARE_BOB_LOWER = re.compile(r"(?<![A-Za-z0-9_.\-])bob(?![A-Za-z0-9_\-]|\.[A-Za-z0-9_])")

#: Where the bare word is looked for: every product tree and the root
#: documents. `agent_pack` is included — it is what enrolled projects read.
BARE_ROOTS = ("host/", "panel/", "ios/", "vscode-extension/", "relay/",
              "relay-ws/", "tools/", ".claude/", ".agents/", ".codex/", "docs/")

#: A person who is not the product: py2app's author, in a licence notice
#: quoted verbatim.
_NOT_THE_PRODUCT = ("Bob Ippolito",)


def _in_bare_scope(rel: str) -> bool:
    if rel == SELF:
        return False
    return rel.startswith(BARE_ROOTS) or ("/" not in rel and rel.endswith(".md"))


def _bare_hits(rel: str, text: str) -> list[str]:
    """One entry per bare word. The whole text is searched, not line by line,
    so the old product's name broken across a line ("Bob" / "Companion") is
    still the old product and not a bare word."""
    scrubbed = text
    for name in _NOT_THE_PRODUCT:
        scrubbed = scrubbed.replace(name, " " * len(name))
    lines = text.splitlines()
    hits = []
    for m in _BARE_BOB.finditer(scrubbed):
        n = scrubbed.count("\n", 0, m.start()) + 1
        hits.append(f"{rel}:{n}: {lines[n - 1].strip()[:100]}")
    return hits


def test_bare_bob_is_gone_everywhere() -> None:
    bad = [hit for rel, text in _current_text_files() if _in_bare_scope(rel)
           for hit in _bare_hits(rel, text)]
    assert not bad, "the bare word is back:\n" + "\n".join(bad[:40])


def test_the_bare_word_check_fires_on_a_doctored_file() -> None:
    rel = "docs/context-board.md"
    real = _text(rel)
    assert "Dark Army as launcher" in real
    assert _bare_hits(rel, real) == []
    doctored = real.replace("Dark Army as launcher", "Bob as launcher", 1)
    assert len(_bare_hits(rel, doctored)) == 1
    # Comments and docstrings count as much as prose.
    assert _bare_hits("x.py", "# ask Bob first\n")
    assert _bare_hits("x.py", '"""Bob\'s own record."""\n')
    assert _bare_hits("x.md", "a Bob-owned terminal\n")
    # Identifiers and the old product's full name are not the bare word.
    for fine in ("BobPanel", "X-Bob-Token", "BobCompanionApp", "Bob Companion.app",
                 "Bob\n    Companion", "Copyright (c) 2004 Bob Ippolito."):
        assert _bare_hits("x.py", fine) == [], fine


# --- (c) the old product is named only where it still has a reason ---------
#: Every file allowed to say "Bob Companion", with the most mentions it may
#: carry. The migration code retired on 23 Sep 2026; what is left is the
#: Codex title-ownership marker on the closed list, one history sentence and
#: the editor extension's pre-move fallback. The ceiling is the count on
#: 23 Sep 2026, so the pile can shrink and never grow without a change here.
LEGACY_MENTION_FILES = (
    "CLAUDE.md",                                     # the closed list: the Codex marker
    "docs/context-host.md",                          # the Codex title-ownership marker
    "docs/first-run-checklist.md",                   # the one history sentence
    "host/dark_army_menubar/hooks.py",               # the Codex marker and its regex
    "vscode-extension/src/extension.ts",             # the pre-move folder fallback
    SELF,
)
_MENTION_CEILING = {
    "CLAUDE.md": 1,
    "docs/context-host.md": 1,
    "docs/first-run-checklist.md": 1,
    "host/dark_army_menubar/hooks.py": 2,
    "vscode-extension/src/extension.ts": 2,
}
_OLD_PRODUCT = re.compile(r"Bob\s+Companion")


def _old_product_problems(files) -> list[str]:
    problems = []
    for rel, text in files:
        if rel == SELF:
            continue
        count = len(_OLD_PRODUCT.findall(text))
        if not count:
            continue
        if rel not in LEGACY_MENTION_FILES:
            problems.append(f"{rel}: says 'Bob Companion' {count}x and carries no upgrade")
        elif count > _MENTION_CEILING[rel]:
            problems.append(f"{rel}: {count} mentions, over its ceiling of {_MENTION_CEILING[rel]}")
    return problems


def test_the_old_product_is_named_only_where_the_upgrade_lives() -> None:
    assert set(_MENTION_CEILING) == set(LEGACY_MENTION_FILES) - {SELF}
    problems = _old_product_problems(_current_text_files())
    assert not problems, "\n".join(problems)


def test_the_old_product_check_fires_on_a_doctored_file() -> None:
    rel = "host/dark_army_menubar/preferences.py"
    real = _text(rel)
    assert _old_product_problems([(rel, real)]) == []
    doctored = real.replace("Dark Army", "Bob Companion", 1)
    assert _old_product_problems([(rel, doctored)])
    listed = "host/dark_army_menubar/hooks.py"
    assert _old_product_problems([(listed, _text(listed))]) == []
    grown = _text(listed) + "\n# Bob Companion\n"
    assert _old_product_problems([(listed, grown)])


# --- (d) the old packages survive only in history -------------------------
#: Exactly the files that may name `bob_companion_daemon` /
#: `bob_companion_menubar`: the broker contract's dated incident line, and
#: this guard.
LEGACY_MODULE_FILES = (
    "docs/pty-broker-contract.md",                   # the 12 Sep 2026 incident
    SELF,
)


def _old_package_files(files) -> set[str]:
    return {rel for rel, text in files if "bob_companion_" in text}


def test_the_old_packages_survive_only_as_compatibility_constants() -> None:
    """The name is historical: since 23 Sep 2026 no constant recognises the
    old packages, and the only survivor is the incident line."""
    found = _old_package_files(_current_text_files())
    assert found == set(LEGACY_MODULE_FILES), (
        f"new: {sorted(found - set(LEGACY_MODULE_FILES))}; "
        f"gone (drop them from the list): {sorted(set(LEGACY_MODULE_FILES) - found)}")


def test_the_old_package_check_fires_on_a_doctored_file() -> None:
    rel = "host/launcher.py"
    real = _text(rel)
    assert "dark_army_menubar" in real
    assert _old_package_files([(rel, real)]) == set()
    doctored = real.replace("dark_army_menubar", "bob_companion_menubar", 1)
    assert _old_package_files([(rel, doctored)]) == {rel}


# --- (e) the hyphenated name is a closed set --------------------------------
#: The only shapes `bob-companion` may take outside history.
_HYPHENATED_OK = re.compile("|".join((
    r"\.bob-companion",                                   # the key folder; ~/.bob-companion
    r"com\.bob-companion\.",                              # bundle ids
    # The Codex MCP name, the helper names the pre-move fallbacks, the ship
    # preflight and the paths test still name, and `ide` for the daemon-probe
    # skill's `grep` of the installed extensions.
    r"bob-companion-(?:board|notify|shunt|statusline|close-out|channel|ide)\b",
    r"x-bob-companion-authorization",                     # the extension's header
    r"androszr/bob-companion",                            # the GitHub repository
    # The Grok leader's client name from before the rename, which
    # `tools/grok_leader_probe.py` still looks for beside the new one.
    r"bob-companion(?=[|,]dark-army)",
    r"name=bob-companion\b",
)))
def _hyphen_hits(rel: str, text: str) -> list[str]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if "bob-companion" not in line:
            continue
        spans = [m.span() for m in _HYPHENATED_OK.finditer(line)]
        for m in re.finditer("bob-companion", line):
            if not any(s <= m.start() < e for s, e in spans):
                hits.append(f"{rel}:{n}: {line.strip()[:100]}")
                break
    return hits


def test_bob_companion_hyphenated_is_a_closed_set() -> None:
    bad = [hit for rel, text in _current_text_files() if rel != SELF
           for hit in _hyphen_hits(rel, text)]
    assert not bad, "bob-companion as a label:\n" + "\n".join(bad[:40])


def test_the_hyphenated_check_fires_on_a_doctored_file() -> None:
    rel = "host/dark_army_daemon/alerts.py"
    real = _text(rel)
    assert "dark-army · feat/panel" in real
    assert _hyphen_hits(rel, real) == []
    doctored = real.replace("dark-army · feat/panel", "bob-companion · feat/panel", 1)
    assert len(_hyphen_hits(rel, doctored)) == 1
    for fine in ("~/.bob-companion/key", "com.bob-companion.panel",
                 "bob-companion-notify"):
        assert _hyphen_hits("x.py", fine) == [], fine
    # The migration's own shapes retired with it (23 Sep 2026).
    for retired in (".migrated-from-bob-companion", "bob-companion.log.2026-09-20.gz",
                    "bob-companion.json", "bob-companion.bob-companion-ide",
                    "the bob-companion-* helpers"):
        assert _hyphen_hits("x.py", retired), retired


# --- (f) no tracked path says the old package -------------------------------
def test_no_tracked_path_says_bob_companion() -> None:
    bad = [rel for rel in _tracked() if "bob_companion" in rel]
    assert not bad, bad[:20]
    assert (REPO / "host/dark_army_daemon/__init__.py").is_file()
    assert (REPO / "host/dark_army_menubar/__init__.py").is_file()
    assert not (REPO / "host/bob_companion_daemon").exists()
    assert not (REPO / "host/bob_companion_menubar").exists()


# --- (l) no tracked path carries the old mascot's name ---------------------
#: A basename beginning `bob-` names a file of the retired mascot (the
#: screen captures under assets/captures/, deleted 23 Sep 2026). The closed
#: list in `PROTECTED` holds no such filename, so none may be tracked.
def _old_mascot_paths(paths) -> list[str]:
    return [rel for rel in paths
            if not _HISTORY.match(rel)
            and rel.rsplit("/", 1)[-1].startswith("bob-")]


def test_no_tracked_path_carries_the_old_mascot_name() -> None:
    assert _old_mascot_paths(_tracked()) == []
    assert not (REPO / "assets/captures").exists()


def test_the_old_mascot_path_check_fires_on_a_doctored_list() -> None:
    fine = ("assets/cast/manifest.json", "host/dark_army_daemon/daemon.py",
            "assets/portraits/cipher.png", "docs/menubar-strip-contract.md")
    assert _old_mascot_paths(fine) == []
    assert _old_mascot_paths(fine + ("assets/captures/bob-idle.gif",)) == [
        "assets/captures/bob-idle.gif"]
    # History is exempt, as everywhere else in this file.
    assert _old_mascot_paths(("plans/2026-09-22-bob-note.md",)) == []


# --- (g) every log channel is Dark Army's -----------------------------------
_OLD_LOGGER = re.compile(r"""getLogger\(\s*["']bob""")


def _old_logger_hits(rel: str, text: str) -> list[str]:
    return [f"{rel}:{n}: {line.strip()}" for n, line in enumerate(text.splitlines(), 1)
            if _OLD_LOGGER.search(line)]


def test_log_channels_say_dark_army() -> None:
    bad = [hit for rel, text in _current_text_files()
           if rel.startswith(("host/dark_army_daemon/", "host/dark_army_menubar/"))
           and rel.endswith(".py")
           for hit in _old_logger_hits(rel, text)]
    assert not bad, "\n".join(bad)


def test_the_log_channel_check_fires_on_a_doctored_file() -> None:
    rel = "host/dark_army_menubar/launchd.py"
    real = _text(rel)
    assert 'getLogger("dark-army.launchd")' in real
    assert _old_logger_hits(rel, real) == []
    doctored = real.replace('getLogger("dark-army.launchd")', 'getLogger("bob-companion.launchd")')
    assert len(_old_logger_hits(rel, doctored)) == 1


# --- (h) no surface string says bob -----------------------------------------
PY_ROOTS = ("host/dark_army_daemon", "host/dark_army_menubar")
SWIFT_ROOTS = ("panel/Sources/BobPanel", "ios/BobPhone", "ios/BobPhoneWidget", "ios/Shared")

#: Whole literals that are a wire or stored token, never drawn. Exact match
#: only — a substring rule for "bob" would allow the entire file.
ALLOWED_EXACT = {
    ("host/dark_army_daemon/channel_server.py", "bob"),   # LEGACY_NAME, the window's alias
    ("host/dark_army_daemon/daemon_board.py", "bob"),     # card_messages author
}
#: Fragments that are themselves wire markers wherever they appear.
_WIRE_FRAGMENTS = ('source="bob"',)


def _python_surface_literals(path: Path):
    """(lineno, value) for every non-docstring string constant."""
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and ast.get_docstring(node, clean=False) is not None:
                docstrings.add(id(node.body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                yield node.lineno, node.value


def _swift_surface_literals(path: Path):
    """(lineno, value) for every double-quoted literal outside a comment."""
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        i, n = 0, len(line)
        while i < n:
            ch = line[i]
            if ch == "/" and i + 1 < n and line[i + 1] == "/":
                break
            if ch == '"':
                j, buf = i + 1, []
                while j < n and line[j] != '"':
                    if line[j] == "\\":
                        j += 1
                    if j < n:
                        buf.append(line[j])
                    j += 1
                yield lineno, "".join(buf)
                i = j + 1
                continue
            i += 1


def _offenders():
    bad = []
    for root, reader, glob in (
        (PY_ROOTS, _python_surface_literals, "*.py"),
        (SWIFT_ROOTS, _swift_surface_literals, "*.swift"),
    ):
        for r in root:
            for path in sorted((REPO / r).rglob(glob)):
                if "agent_pack" in path.parts:
                    continue
                rel = path.relative_to(REPO).as_posix()
                for lineno, value in reader(path):
                    probe = value
                    for fragment in _WIRE_FRAGMENTS:
                        probe = probe.replace(fragment, "")
                    if not (_BARE_BOB.search(probe) or _BARE_BOB_LOWER.search(probe)):
                        continue
                    if (rel, value) in ALLOWED_EXACT:
                        continue
                    bad.append(f"{rel}:{lineno}: {value[:90]}")
    return bad


def test_no_surface_string_says_bob() -> None:
    bad = _offenders()
    assert not bad, "surface strings still say bob:\n" + "\n".join(bad)


def test_a_doctored_prompt_line_is_flagged(tmp_path: Path) -> None:
    """`root@bob:` is a bare lowercase word: `@` and `:` are not glue."""
    real = (REPO / "panel/Sources/BobPanel/Theme.swift").read_text()
    assert "root@darkarmy:" in real
    doctored = real.replace("root@darkarmy:", "root@bob:")
    assert doctored != real
    probe = tmp_path / "Theme.swift"
    probe.write_text(doctored)
    hits = [v for _, v in _swift_surface_literals(probe) if _BARE_BOB_LOWER.search(v)]
    assert hits, "a lowercase hostname slipped past the surface-string check"
    assert all("root@bob:" in v for v in hits)
    # A dot glues only when an identifier character follows it.
    assert _BARE_BOB_LOWER.search("ask bob.")
    assert _BARE_BOB_LOWER.search("ask bob. then")
    # And the glue rule holds: identifiers are not words.
    for ident in ("bob-card", "bob.panelOpenBoard", ".bob-companion",
                  "bob_add_card", "local.bob.BobPhone", "bob-home v1"):
        assert not _BARE_BOB_LOWER.search(ident), ident


# --- (i) prose a person reads says Dark Army --------------------------------
#: Whole documents a person reads about the product, read with code removed.
PROSE_FILES = (
    "CONTRIBUTING.md",
    "GEMINI.md",
    "AGENTS.md",
    "relay/README.md",
    "relay-ws/README.md",
    "vscode-extension/README.md",
    "ios/README.md",
    # The front page and the two pages its detail moved to (22 Sep 2026).
    "README.md",
    "docs/reference.md",
    "SECURITY.md",
)
_FENCE = re.compile(r"^```.*?^```", re.S | re.M)
_SPAN = re.compile(r"`[^`\n]*`")


def _prose(rel: str) -> str:
    """The file's text with fenced blocks and backtick spans removed — code
    and identifiers are the other direction's business."""
    text = (REPO / rel).read_text(encoding="utf-8")
    return _SPAN.sub("", _FENCE.sub("", text))


@pytest.mark.parametrize("rel", PROSE_FILES)
def test_prose_files_say_dark_army(rel: str) -> None:
    bad = [f"{rel}:{n}: {line.strip()[:90]}"
           for n, line in enumerate(_prose(rel).splitlines(), 1)
           if _BARE_BOB.search(line) or _BARE_BOB_LOWER.search(line)
           or _OLD_PRODUCT.search(line)]
    assert not bad, "prose still says the old name:\n" + "\n".join(bad)


def test_the_prose_scan_sees_through_nothing_but_code() -> None:
    """A bare word in prose is caught; the same word in a code span or a
    fence is not."""
    assert _BARE_BOB.search(_SPAN.sub("", "Open Bob's panel"))
    assert not _BARE_BOB.search(_SPAN.sub("", "the `BobPanel` bundle"))
    assert not _BARE_BOB.search(_FENCE.sub("", "```\nBob\n```\n"))


# --- (j) the display names --------------------------------------------------
def test_display_names() -> None:
    setup = (REPO / "host/setup.py").read_text()
    # The bundle filename and the install path are derived from these, and
    # moved on 22 Sep 2026 with a first-launch migration behind them; the
    # identifier did not.
    assert '"CFBundleName": "Dark Army"' in setup
    assert 'name="Dark Army"' in setup
    assert '"CFBundleDisplayName": "Dark Army"' in setup
    assert '"CFBundleIdentifier": "com.bob-companion.menubar"' in setup

    panel = plistlib.loads((REPO / "panel/Sources/BobPanel/Info.plist").read_bytes())
    assert panel["CFBundleName"] == "Dark Army"
    assert panel["CFBundleDisplayName"] == "Dark Army"
    assert panel["CFBundleIdentifier"] == "com.bob-companion.panel"
    assert panel["CFBundleExecutable"] == "BobPanel"

    for rel in ("ios/BobPhone/Info.plist", "ios/BobPhoneWidget/Info.plist"):
        d = plistlib.loads((REPO / rel).read_bytes())
        assert d["CFBundleDisplayName"] == "Dark Army", rel

    pkg = json.loads((REPO / "vscode-extension/package.json").read_text())
    assert pkg["name"] == "dark-army-ide"
    assert pkg["publisher"] == "dark-army"
    assert pkg["displayName"] == "Dark Army IDE Bridge"
    commands = [c["command"] for c in pkg["contributes"]["commands"]]
    assert commands == ["darkArmy.showThisSession"]
    # The old id is a hidden alias: registered in the code, never contributed.
    assert "bobCompanion.showThisSession" not in commands
    assert pkg["contributes"]["keybindings"][0]["command"] \
        == "darkArmy.showThisSession"

    ext = (REPO / "vscode-extension/src/extension.ts").read_text()
    assert f"EXT_VERSION = '{pkg['version']}'" in ext

    widget = (REPO / "ios/BobPhoneWidget/BobPhoneWidget.swift").read_text()
    assert 'kind: "BobFleetTile"' in widget


# --- (k) no preference key carries the product name -------------------------
EXPECTED_PREFERENCE_KEYS = {
    "session_timeout",
    "session_title",
    "notification_sound",
    "notification_banners",
    "channel_enabled",
    "typed_reply",
    "auto_compact",
    "board_dispatch",
    "board_autostart",
    "board_parallel",
    "board_parallel_by_root",
    "board_isolation_by_root",
    "agent_models",
    "agent_models_by_root",
    "agent_efforts",
    "agent_efforts_by_root",
    "panel_scale",
    "board_close_terminal",
    "board_own_terminal",
    "lan_access",
    "remote_access",
    "relay_ws",
    "phone_push",
    "dictation_shortcut",
}


def test_preference_keys_unchanged() -> None:
    sys.path.insert(0, str(REPO / "host"))
    from dark_army_menubar import preferences

    assert set(preferences.DEFAULTS) == EXPECTED_PREFERENCE_KEYS
    blob = " ".join(str(k) + " " + str(v) for k, v in preferences.DEFAULTS.items())
    assert "bob" not in blob.lower()
