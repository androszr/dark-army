"""Shape, forward compatibility, and the lock of agent-pack.json."""

from __future__ import annotations

import json
import stat
import threading

import pytest

from dark_army_daemon import paths
from dark_army_menubar import pack_install, pack_ledger


@pytest.fixture(autouse=True)
def ledger_path(tmp_path, monkeypatch):
    path = tmp_path / "agent-pack.json"
    monkeypatch.setattr(paths, "AGENT_PACK_PATH", path)
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path)
    return path


def test_unknown_key_survives_a_write(tmp_path, ledger_path):
    pack_ledger.remember(
        str(tmp_path / "a"), profile="web", prefix="aa", project="a")
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    data["extra"] = "keep"
    data["projects"][0]["mystery"] = 1
    ledger_path.write_text(json.dumps(data), encoding="utf-8")
    pack_ledger.remember(
        str(tmp_path / "a"), last_result="ok")
    out = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert out["extra"] == "keep"
    assert out["projects"][0]["mystery"] == 1
    assert out["projects"][0]["last_result"] == "ok"


def test_absent_key_defaulted_on_read(tmp_path, ledger_path):
    ledger_path.write_text(
        json.dumps({"version": 1, "projects": [{"root": "/x"}]}),
        encoding="utf-8",
    )
    published = pack_ledger.published()
    assert published == [{
        "root": "/x",
        "profile": "",
        "last_sync_at": 0.0,
        "last_result": "",
    }]


def test_file_mode_is_0600(tmp_path, ledger_path):
    pack_ledger.remember(
        str(tmp_path / "a"), profile="web", prefix="aa", project="a")
    mode = ledger_path.stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_max_projects_refused(tmp_path):
    for i in range(pack_ledger.MAX_PACK_PROJECTS):
        pack_ledger.remember(
            str(tmp_path / f"p{i}"), profile="web", prefix="xx",
            project=f"p{i}")
    with pytest.raises(pack_ledger.PackLedgerError, match="already syncing"):
        pack_ledger.remember(
            str(tmp_path / "overflow"), profile="web", prefix="xx",
            project="overflow")


def test_remember_refuses_bobs_own(tmp_path, monkeypatch):
    monkeypatch.setattr(pack_install, "_is_bobs_own", lambda root: True)
    with pytest.raises(pack_ledger.PackLedgerError, match="own project"):
        pack_ledger.remember(
            str(tmp_path / "bob"), profile="web", prefix="bc", project="bob")
    assert pack_ledger.published() == []


def test_update_does_not_insert(tmp_path):
    assert pack_ledger.update(
        str(tmp_path / "ghost"), last_result="x") is None
    assert pack_ledger.published() == []


def test_reserve_then_forget_then_update_stays_gone(tmp_path):
    root = str(tmp_path / "proj")
    pack_ledger.reserve(root, profile="web", prefix="xx", project="proj")
    assert pack_ledger.forget(root) is True
    assert pack_ledger.update(root, last_result="ok") is None
    assert pack_ledger.entry(root) is None


def test_forget_removes_entry_and_writes_nothing_else(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "CLAUDE.md").write_text("stay\n", encoding="utf-8")
    pack_ledger.remember(
        str(root), profile="web", prefix="xx", project="proj")
    assert pack_ledger.forget(str(root)) is True
    assert pack_ledger.entry(str(root)) is None
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == "stay\n"
    assert pack_ledger.forget(str(root)) is False


def test_concurrent_writes_keep_both_entries(tmp_path):
    a = str(tmp_path / "a")
    b = str(tmp_path / "b")
    errors = []

    def one(root, prefix):
        try:
            pack_ledger.remember(
                root, profile="web", prefix=prefix, project=prefix)
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=one, args=(a, "aa")),
        threading.Thread(target=one, args=(b, "bb")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    data = json.loads(paths.AGENT_PACK_PATH.read_text(encoding="utf-8"))
    roots = {row["root"] for row in data["projects"]}
    assert len(roots) == 2


def test_gitignore_offered_defaults_and_round_trips(tmp_path, ledger_path):
    """A row an older build wrote has no `gitignore_offered` and reads as
    never offered; `update` stores the list and `remember` threads it."""
    ledger_path.write_text(
        json.dumps({"version": 1, "projects": [
            {"root": str(tmp_path / "old"), "profile": "web", "prefix": "xx"}]}),
        encoding="utf-8",
    )
    row = pack_ledger.entry(str(tmp_path / "old"))
    assert (row.get("gitignore_offered") or []) == []
    pack_ledger.update(str(tmp_path / "old"), gitignore_offered=["plans", ".env"])
    assert pack_ledger.entry(str(tmp_path / "old"))["gitignore_offered"] == [
        "plans", ".env"]
    pack_ledger.remember(str(tmp_path / "new"), profile="web", prefix="xx",
                         project="new", gitignore_offered=["plans"])
    assert pack_ledger.entry(str(tmp_path / "new"))["gitignore_offered"] == ["plans"]
    assert "gitignore_offered" not in pack_ledger.published()[0]


def test_settings_deny_owned_defaults_to_none_and_round_trips(tmp_path, ledger_path):
    """A row an older build wrote has no `settings_deny_owned` and reads as
    none owned, so the first resync adds the guard rows and removes none."""
    ledger_path.write_text(
        json.dumps({"version": 1, "projects": [
            {"root": str(tmp_path / "old"), "profile": "web", "prefix": "xx"}]}),
        encoding="utf-8",
    )
    assert pack_install._owned_for(str(tmp_path / "old"), "settings_deny_owned") == []
    pack_ledger.update(str(tmp_path / "old"),
                       settings_deny_owned=["Read(**/.dark-army/key)"])
    assert pack_ledger.entry(str(tmp_path / "old"))["settings_deny_owned"] == [
        "Read(**/.dark-army/key)"]
    assert "settings_deny_owned" not in pack_ledger.published()[0]
