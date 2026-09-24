"""The folders an agent may search: the file `search_scope.py` writes, and the
four places the rule is carried.

Every test points the ledger, the state folder, the scope file and Dark
Army's own root at `tmp_path` — `test_enrollment.py`'s seam — so nothing here
reads or writes the real `~/.dark-army`.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from dark_army_daemon import enrollment, paths, search_scope

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def scope(tmp_path, monkeypatch, enforce_enrolment):
    state = tmp_path / "state"
    path = state / "search-scope.json"
    monkeypatch.setattr(paths, "ENROLLMENT_PATH", tmp_path / "enrollment.json")
    monkeypatch.setattr(paths, "STATE_DIR", state)
    monkeypatch.setattr(paths, "SEARCH_SCOPE_PATH", path)
    monkeypatch.setattr(enrollment, "_SELF_ROOT", str(tmp_path / "dark-army"))
    enrollment.invalidate()
    yield path
    enrollment.invalidate()


def _project(tmp_path, name="proj"):
    root = tmp_path / name
    (root / "src").mkdir(parents=True)
    (root / ".git").mkdir()
    return root


def _roots(path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))["roots"]


# ── the composition ───────────────────────────────────────────────────────────

def test_compose_sorts_dedupes_and_drops_empty_strings():
    data = search_scope.compose({"/b", "/a", ""}, "/a", "/state")
    assert data == {"version": 1, "roots": ["/a", "/b", "/state"]}
    assert set(data) == {"version", "roots"}


def test_compose_with_no_checkout_keeps_the_state_folder():
    """A release install has no source tree: `self_root()` is ""."""
    data = search_scope.compose(set(), "", "/state")
    assert data["roots"] == ["/state"]


def test_compose_ignores_anything_but_folder_strings():
    data = search_scope.compose(["/a", None, 7, "  "], "/a", "/s")
    assert data["roots"] == ["/a", "/s"]


# ── the writer ────────────────────────────────────────────────────────────────

def test_refresh_writes_the_roots_privately(scope, tmp_path):
    assert search_scope.refresh() is True
    assert stat.S_IMODE(os.stat(scope).st_mode) == 0o600
    assert _roots(scope) == sorted([str(tmp_path / "dark-army"),
                                    str(paths.STATE_DIR)])
    assert search_scope.roots() == _roots(scope)


def test_a_second_refresh_with_nothing_changed_writes_nothing(scope):
    assert search_scope.refresh() is True
    before = os.stat(scope).st_mtime_ns
    inode = os.stat(scope).st_ino
    os.utime(scope, ns=(before - 10_000_000_000, before - 10_000_000_000))
    stamped = os.stat(scope).st_mtime_ns
    assert search_scope.refresh() is True
    assert os.stat(scope).st_mtime_ns == stamped
    assert os.stat(scope).st_ino == inode


def test_an_unwritable_scope_file_is_false_and_never_raises(scope, tmp_path):
    scope.mkdir(parents=True)  # a directory where the file should be
    assert search_scope.refresh() is False
    # And the ledger write it follows keeps its own answer.
    assert enrollment.save({"projects": []}) is True


def test_the_private_ring_names_the_scope_file():
    assert paths.SEARCH_SCOPE_PATH.name in paths._PRIVATE_FILES


# ── the round trip through the ledger ─────────────────────────────────────────

def test_enrol_and_unenrol_move_the_list(scope, tmp_path):
    root = _project(tmp_path)
    ok, _ = enrollment.enroll(str(root))
    assert ok
    (enrolled,) = enrollment.enrolled_roots()
    assert enrolled in _roots(scope)
    ok, _ = enrollment.unenroll(enrolled)
    assert ok
    assert enrolled not in _roots(scope)
    assert str(tmp_path / "dark-army") in _roots(scope)


def test_the_file_holds_no_digest_and_no_key(scope, tmp_path):
    root = _project(tmp_path)
    assert enrollment.enroll(str(root))[0]
    text = scope.read_text(encoding="utf-8")
    key = (root / ".dark-army" / "key").read_text(encoding="utf-8").strip()
    assert key and key not in text
    # As a key: the folder names themselves may say anything (this test's
    # own tmp_path contains the word).
    assert '"digest"' not in text
    for entry in enrollment.projects():
        assert entry["digest"] not in text
    assert set(json.loads(text)) == {"version", "roots"}


# ── the four carriers ─────────────────────────────────────────────────────────

def test_the_rule_rides_all_four_carriers():
    from dark_army_menubar import pack_render
    from dark_army_menubar.hooks import GROK_RULES_TEXT, NOTIFY_SCRIPT

    assert "SEARCH_SCOPE_HINT" in NOTIFY_SCRIPT
    assert "Where to search" in GROK_RULES_TEXT
    assert "search-scope.json" in GROK_RULES_TEXT
    rendered = pack_render.render("web", "abc", "proj")["AGENTS.md"].decode()
    assert "## Where to search" in rendered
    assert "search-scope.json" in rendered
    own = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Where to search" in own
    assert "search-scope.json" in own
