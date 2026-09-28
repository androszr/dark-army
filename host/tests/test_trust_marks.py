"""Copying a project's folder trust onto its card worktrees (`trust_marks.py`).

Fake config files under `tmp_path`: a decision is copied only from the
root's own entry, never invented; every foreign key and table survives; a
symlink is left alone; `unmark` takes back exactly what `mark` wrote.
`docs/card-worktrees.md`, *The trust copies*.
"""

import json
import os
import tomllib

import pytest

from dark_army_daemon import paths, trust_marks

ROOT = "/Users/p/proj"
WT = "/Users/p/proj/.worktrees/card-abcd1234"


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    codex = tmp_path / "codex" / "config.toml"
    codex.parent.mkdir()
    claude = tmp_path / "claude.json"
    monkeypatch.setattr(trust_marks, "CODEX_CONFIG_PATH", codex)
    monkeypatch.setattr(trust_marks, "CLAUDE_CONFIG_PATH", claude)
    return codex, claude


def _codex_trusted():
    return ('model = "gpt"\n\n[projects."/Users/p/proj"]\n'
            'trust_level = "trusted"\n\n[projects."/elsewhere"]\n'
            'trust_level = "untrusted"\n')


def _claude_trusted():
    return json.dumps({
        "numStartups": 3,
        "projects": {
            ROOT: {"hasTrustDialogAccepted": True, "allowedTools": ["x"]},
            "/elsewhere": {"hasTrustDialogAccepted": False},
        },
    }, indent=2) + "\n"


def test_the_default_paths_never_name_the_real_home():
    assert str(trust_marks.CODEX_CONFIG_PATH).startswith(str(paths._home()))
    assert str(trust_marks.CLAUDE_CONFIG_PATH).startswith(str(paths._home()))


def test_a_trusted_root_is_copied_onto_the_worktree(fake):
    codex, claude = fake
    codex.write_text(_codex_trusted())
    claude.write_text(_claude_trusted())
    assert trust_marks.mark(ROOT, WT) == {"codex": True, "claude": True}
    data = tomllib.loads(codex.read_text())
    assert data["projects"][WT]["trust_level"] == "trusted"
    assert trust_marks.MARKER in codex.read_text()
    entry = json.loads(claude.read_text())["projects"][WT]
    assert entry == {"hasTrustDialogAccepted": True}


def test_an_untrusted_or_unknown_root_writes_nothing(fake):
    codex, claude = fake
    codex_text = 'model = "gpt"\n\n[projects."/Users/p/proj"]\ntrust_level = "untrusted"\n'
    claude_text = json.dumps({"projects": {"/other": {
        "hasTrustDialogAccepted": True}}}, indent=2)
    codex.write_text(codex_text)
    claude.write_text(claude_text)
    assert trust_marks.mark(ROOT, WT) == {"codex": False, "claude": False}
    assert codex.read_text() == codex_text
    assert claude.read_text() == claude_text


def test_missing_files_are_never_created(fake):
    codex, claude = fake
    assert trust_marks.mark(ROOT, WT) == {"codex": False, "claude": False}
    assert not codex.exists()
    assert not claude.exists()


def test_foreign_keys_and_tables_survive(fake):
    codex, claude = fake
    codex.write_text(_codex_trusted())
    claude.write_text(_claude_trusted())
    trust_marks.mark(ROOT, WT)
    data = tomllib.loads(codex.read_text())
    assert data["model"] == "gpt"
    assert data["projects"]["/elsewhere"]["trust_level"] == "untrusted"
    whole = json.loads(claude.read_text())
    assert whole["numStartups"] == 3
    assert whole["projects"][ROOT]["allowedTools"] == ["x"]
    assert whole["projects"]["/elsewhere"] == {"hasTrustDialogAccepted": False}


def test_an_existing_worktree_entry_keeps_its_keys(fake):
    _codex, claude = fake
    data = json.loads(_claude_trusted())
    data["projects"][WT] = {"history": ["a"]}
    claude.write_text(json.dumps(data, indent=2))
    trust_marks.mark(ROOT, WT)
    assert json.loads(claude.read_text())["projects"][WT] == {
        "history": ["a"], "hasTrustDialogAccepted": True}


def test_an_existing_codex_table_for_the_worktree_is_never_touched(fake):
    codex, _claude = fake
    text = _codex_trusted() + f'\n[projects."{WT}"]\ntrust_level = "untrusted"\n'
    codex.write_text(text)
    assert trust_marks.mark(ROOT, WT)["codex"] is False
    assert codex.read_text() == text
    assert trust_marks.unmark(WT)["codex"] is False
    assert codex.read_text() == text


def test_unmark_after_mark_restores_the_original_bytes(fake):
    codex, claude = fake
    codex.write_text(_codex_trusted())
    claude.write_text(_claude_trusted())
    trust_marks.mark(ROOT, WT)
    assert trust_marks.unmark(WT) == {"codex": True, "claude": True}
    assert codex.read_text() == _codex_trusted()
    assert claude.read_text() == _claude_trusted()


def test_unmark_leaves_an_entry_claude_has_since_written_to(fake):
    _codex, claude = fake
    claude.write_text(_claude_trusted())
    trust_marks.mark(ROOT, WT)
    data = json.loads(claude.read_text())
    data["projects"][WT]["lastCost"] = 1.5
    claude.write_text(json.dumps(data, indent=2))
    assert trust_marks.unmark(WT)["claude"] is False
    assert json.loads(claude.read_text())["projects"][WT]["lastCost"] == 1.5


def test_a_symlinked_file_is_left_alone(fake, tmp_path):
    codex, claude = fake
    real = tmp_path / "real.toml"
    real.write_text(_codex_trusted())
    codex.symlink_to(real)
    real_json = tmp_path / "real.json"
    real_json.write_text(_claude_trusted())
    claude.symlink_to(real_json)
    assert trust_marks.mark(ROOT, WT) == {"codex": False, "claude": False}
    assert real.read_text() == _codex_trusted()
    assert real_json.read_text() == _claude_trusted()


def test_an_unparseable_file_is_left_alone(fake):
    codex, claude = fake
    codex.write_text("[projects\nnot toml")
    claude.write_text("{not json")
    assert trust_marks.mark(ROOT, WT) == {"codex": False, "claude": False}
    assert codex.read_text() == "[projects\nnot toml"
    assert claude.read_text() == "{not json"


def test_the_write_is_atomic_and_keeps_the_mode(fake):
    codex, claude = fake
    codex.write_text(_codex_trusted())
    os.chmod(codex, 0o640)
    before = os.stat(codex).st_ino
    trust_marks.mark(ROOT, WT)
    assert os.stat(codex).st_mode & 0o777 == 0o640
    # Replaced through a sibling, not rewritten in place.
    assert os.stat(codex).st_ino != before
    assert [p.name for p in codex.parent.iterdir()] == ["config.toml"]


def test_root_trusted_reads_the_assistant_being_started(fake):
    codex, claude = fake
    assert not trust_marks.root_trusted(ROOT, "claude")
    assert not trust_marks.root_trusted(ROOT, "codex")
    codex.write_text(_codex_trusted())
    claude.write_text(_claude_trusted())
    assert trust_marks.root_trusted(ROOT, "claude")
    assert trust_marks.root_trusted(ROOT, "codex")
    assert not trust_marks.root_trusted("/elsewhere", "claude")
    assert not trust_marks.root_trusted("/elsewhere", "codex")
    # An assistant with no trust record Dark Army can read never qualifies.
    assert not trust_marks.root_trusted(ROOT, "grok")


def test_unmark_leaves_the_codex_file_alone_when_the_removal_would_not_parse(fake):
    codex, _claude = fake
    base = ('[projects."/elsewhere"]\ntrust_level = "untrusted"\n'
            '\n[projects."/Users/p/proj"]\n'
            'trust_level = "trusted"\nsandbox = "b"\n')
    codex.write_text(base)
    trust_marks.mark(ROOT, WT)
    # The person added a key under Dark Army's table; removing the table
    # would hand that key to the table above, where it already exists.
    text = codex.read_text().replace(
        'trust_level = "trusted" ' + trust_marks.MARKER + "\n",
        'trust_level = "trusted" ' + trust_marks.MARKER + '\nsandbox = "a"\n')
    codex.write_text(text)
    tomllib.loads(text)
    moved = text.replace(f'\n[projects."{WT}"]\n', "\n", 1)
    assert trust_marks.unmark(WT)["codex"] is False
    assert codex.read_text() == text
    assert moved != text
