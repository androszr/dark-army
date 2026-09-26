# host/tests/test_plan_index.py
"""The plan index and body read (`plan_index.py`): pure over a `tmp_path`
tree — the day-then-mtime-then-path order, the dated-name rule that leaves
the folder's readme and question list out, the per-root cap, the cards'
`plan_path` join, the closed set, and no body on any row."""

import os
from pathlib import Path

import pytest

from dark_army_daemon import plan_index, scout_report
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon_board import BoardVerbsMixin

REFUSAL = BoardVerbsMixin._plan_path_refusal

PLAN = """# The phone lists plans

- **Date:** 2026-09-25
- **Status:** draft
- **Area:** pocket

## What this does

Lists plans.

- **Status:** not the header
"""

BARE = """# A plan with no header list

## Steps

1. Do it.
"""


def _write(path: Path, text: str, mtime: float = None) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return os.path.realpath(path)


@pytest.fixture
def tree(tmp_path):
    a = tmp_path / "alpha"
    b = tmp_path / "beta"
    a.mkdir()
    b.mkdir()
    root_a = os.path.realpath(a)
    root_b = os.path.realpath(b)
    plans = a / "plans"
    paths = {
        # Newer day, older mtime: the day wins.
        "new_day": _write(plans / "2026-09-25-phone-plans.md", PLAN,
                          1_000_100),
        "old_day": _write(plans / "2026-09-20-older.md", BARE, 1_000_900),
        # Same day as new_day, newer mtime: ahead of it.
        "same_day": _write(b / "plans" / "2026-09-25-beta-plan.md", BARE,
                           1_000_500),
        "readme": _write(plans / "README.md", PLAN, 1_001_000),
        "questions": _write(plans / "answerable-questions.md", PLAN,
                            1_001_000),
        "txt": _write(plans / "2026-09-26-notes.txt", PLAN, 1_001_000),
        "undated": _write(plans / "someday-plan.md", PLAN, 1_001_000),
    }
    (plans / "2026-09-27-a-folder.md").mkdir()
    return root_a, root_b, paths


def _build(roots, cards=()):
    return plan_index.build(roots, list(cards), refusal=REFUSAL)


def _card(root, path, cid="c1", title="Phone plans", column="backlog"):
    return {"id": cid, "title": title, "column_name": column, "root": root,
            "plan_path": path}


def test_rows_are_newest_day_first_then_mtime_then_path(tree):
    root_a, root_b, paths = tree
    index = _build([root_a, root_b])
    got = [r["path"] for r in index["rows"]]
    assert got == [paths["same_day"], paths["new_day"], paths["old_day"]]
    assert index["roots"] == 2
    assert index["truncated"] is False and index["omitted"] == 0


def test_equal_day_and_mtime_falls_back_to_path(tmp_path):
    root = os.path.realpath(tmp_path)
    b = _write(tmp_path / "plans" / "2026-01-01-b.md", BARE, 5)
    a = _write(tmp_path / "plans" / "2026-01-01-a.md", BARE, 5)
    assert [r["path"] for r in _build([root])["rows"]] == [a, b]


def test_non_plans_are_left_out_by_the_name_rule(tree):
    root_a, _root_b, paths = tree
    listed = {r["path"] for r in _build([root_a])["rows"]}
    for key in ("readme", "questions", "txt", "undated"):
        assert paths[key] not in listed, key
    assert not any(p.endswith("a-folder.md") for p in listed)


def test_a_symlinked_file_and_a_fifo_are_skipped(tmp_path):
    root = os.path.realpath(tmp_path)
    outside = _write(tmp_path / "secret.md", PLAN)
    plans = tmp_path / "plans"
    plans.mkdir()
    os.symlink(outside, plans / "2026-09-25-link.md")
    os.mkfifo(plans / "2026-09-25-fifo.md")
    assert _build([root])["rows"] == []


def test_a_symlinked_plans_folder_is_skipped(tmp_path):
    root = os.path.realpath(tmp_path)
    _write(tmp_path / "elsewhere" / "2026-09-25-x.md", PLAN)
    os.symlink(tmp_path / "elsewhere", tmp_path / "plans")
    assert _build([root])["rows"] == []


def test_the_per_root_cap_keeps_the_newest_names(tmp_path, monkeypatch):
    root = os.path.realpath(tmp_path)
    for day in range(1, 6):
        _write(tmp_path / "plans" / f"2026-01-0{day}-p.md", BARE, 100 - day)
    monkeypatch.setattr(plan_index, "MAX_FILES_PER_ROOT", 3)
    days = [r["day"] for r in _build([root])["rows"]]
    assert days == ["2026-01-05", "2026-01-04", "2026-01-03"]


def test_rows_carry_the_header_fields_and_no_body(tree):
    root_a, _root_b, paths = tree
    rows = {r["path"]: r for r in _build([root_a])["rows"]}
    row = rows[paths["new_day"]]
    assert row["title"] == "The phone lists plans"
    assert row["status"] == "draft"
    assert row["area"] == "pocket"
    assert row["name"] == "2026-09-25-phone-plans.md"
    assert row["slug"] == "phone-plans"
    assert row["day"] == "2026-09-25"
    assert row["project"] == "alpha"
    assert row["bytes"] == len(PLAN.encode())
    assert row["card_id"] == "" and row["card_title"] == ""
    bare = rows[paths["old_day"]]
    assert bare["status"] == "" and bare["area"] == ""
    for r in rows.values():
        assert "body" not in r and "text" not in r


def test_an_in_folder_card_plan_annotates_its_row(tree):
    root_a, _root_b, paths = tree
    index = _build([root_a], [_card(root_a, paths["new_day"])])
    assert len(index["rows"]) == 2
    row = next(r for r in index["rows"] if r["path"] == paths["new_day"])
    assert row["card_id"] == "c1"
    assert row["card_title"] == "Phone plans"
    assert row["card_column"] == "backlog"


def test_an_out_of_folder_card_plan_is_its_own_row(tree, tmp_path):
    root_a, _root_b, _paths = tree
    elsewhere = _write(tmp_path / "alpha" / "docs" / "design.md", PLAN, 1)
    index = _build([root_a], [_card(root_a, "docs/design.md")])
    row = next(r for r in index["rows"] if r["path"] == elsewhere)
    assert row["card_id"] == "c1"
    assert row["day"] == "" and row["slug"] == "design"
    # An undated plan sorts after every dated one.
    assert index["rows"][-1]["path"] == elsewhere


def test_a_card_on_an_unenrolled_root_contributes_nothing(tree, tmp_path):
    root_a, _root_b, _paths = tree
    other = tmp_path / "gamma"
    stray = _write(other / "docs" / "p.md", PLAN)
    index = _build([root_a], [_card(os.path.realpath(other), stray)])
    assert stray not in {r["path"] for r in index["rows"]}


def test_two_cards_on_one_plan_take_the_first_in_card_order(tree, tmp_path):
    root_a, _root_b, paths = tree
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    try:
        low, _ = store.create({"title": "Low priority", "root": root_a})
        high, _ = store.create({"title": "High priority", "root": root_a,
                                "priority": "3"})
        for card in (low, high):
            store._conn.execute("UPDATE cards SET plan_path = ? WHERE id = ?",
                                (paths["new_day"], card["id"]))
        cards = store.plans_index_rows()
    finally:
        store.close()
    assert [c["id"] for c in cards] == [high["id"], low["id"]]
    assert set(cards[0]) == {"id", "title", "column_name", "root",
                             "plan_path"}
    row = next(r for r in _build([root_a], cards)["rows"]
               if r["path"] == paths["new_day"])
    assert row["card_id"] == high["id"]


def test_locate_admits_exactly_the_two_shapes(tree, tmp_path):
    root_a, root_b, paths = tree
    elsewhere = _write(tmp_path / "alpha" / "docs" / "design.md", PLAN)
    cards = [_card(root_a, "docs/design.md")]
    roots = [root_a, root_b]
    resolved, root, why = plan_index.locate(paths["new_day"], roots, cards,
                                            refusal=REFUSAL)
    assert (resolved, root, why) == (paths["new_day"], root_a, "")
    resolved, root, why = plan_index.locate(elsewhere, roots, cards,
                                            refusal=REFUSAL)
    assert (resolved, root, why) == (elsewhere, root_a, "")


def test_locate_refuses_everything_else(tree, tmp_path):
    root_a, root_b, paths = tree
    unattached = _write(tmp_path / "alpha" / "docs" / "other.md", PLAN)
    sibling = _write(tmp_path / "gamma" / "plans" / "2026-09-25-x.md", PLAN)
    nested = _write(tmp_path / "alpha" / "plans" / "deep"
                    / "2026-09-25-nested.md", PLAN)
    outside = _write(tmp_path / "secret.md", PLAN)
    link = Path(root_a) / "plans" / "2026-09-24-link.md"
    os.symlink(outside, link)
    traversal = os.path.join(root_a, "plans", "..", "..", "secret.md")
    roots = [root_a, root_b]
    for path in (unattached, sibling, nested, str(link), traversal,
                 paths["txt"], paths["readme"], "", "relative/plan.md",
                 "/etc/hosts", "/" + "x" * 5000):
        assert plan_index.locate(path, roots, [], refusal=REFUSAL) == (
            "", "", plan_index.NOT_LISTED), path


def test_read_strips_the_h1_and_reports_the_header(tree):
    root_a, _root_b, paths = tree
    plan = plan_index.read(paths["new_day"], [root_a], [], refusal=REFUSAL)
    assert plan["available"] is True
    assert plan["reason"] == ""
    assert plan["title"] == "The phone lists plans"
    assert plan["status"] == "draft" and plan["area"] == "pocket"
    assert plan["name"] == "2026-09-25-phone-plans.md"
    assert plan["day"] == "2026-09-25"
    assert plan["project"] == "alpha"
    assert not plan["body"].startswith("# ")
    assert plan["body"].startswith("- **Date:**")
    assert "## What this does" in plan["body"]
    assert plan["written_at"] == 1_000_100


def test_read_outside_the_set_is_not_listed(tree):
    root_a, _root_b, paths = tree
    plan = plan_index.read(paths["readme"], [root_a], [], refusal=REFUSAL)
    assert plan["available"] is False
    assert plan["reason"] == plan_index.NOT_LISTED
    assert plan["body"] == ""


def test_read_over_the_bound_is_unreadable_in_words(tree, monkeypatch):
    root_a, _root_b, paths = tree
    monkeypatch.setattr(scout_report, "MAX_REPORT_BYTES", 16)
    plan = plan_index.read(paths["new_day"], [root_a], [], refusal=REFUSAL)
    assert plan["available"] is False
    assert plan["reason"] == plan_index.UNREADABLE
    assert plan["body"] == ""


def test_this_checkouts_plans_folder_lists_no_non_plans():
    repo = Path(__file__).resolve().parents[2]
    if not (repo / "plans").is_dir():
        pytest.skip("no plans folder in this checkout")
    rows = plan_index.scan_root(str(repo))
    names = {r["name"] for r in rows}
    assert "README.md" not in names
    assert "answerable-questions.md" not in names
    assert all(plan_index.FILE_RE.match(n) for n in names)


def test_a_name_ending_in_a_newline_is_not_a_plan():
    # `$` matches before a trailing newline; the pattern must not.
    from dark_army_daemon import plan_index, scout_index
    assert plan_index.FILE_RE.match("2026-01-01-x.md")
    assert not plan_index.FILE_RE.match("2026-01-01-x.md\n")
    assert scout_index.FOLDER_RE.match("2026-01-01-x")
    assert not scout_index.FOLDER_RE.match("2026-01-01-x\n")
