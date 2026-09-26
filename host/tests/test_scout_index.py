# host/tests/test_scout_index.py
"""The scout-report index and body read (`scout_index.py`): pure over a
`tmp_path` tree — ordering, bounds, the closed set, the header split pinned
equal to `scout_report.parse_header`, and no body on any row."""

import os
from pathlib import Path

import pytest

from dark_army_daemon import scout_index, scout_report
from dark_army_daemon.daemon_board import BoardVerbsMixin

REPO = Path(__file__).resolve().parents[2]
REFUSAL = BoardVerbsMixin._plan_path_refusal

COMPLETE = """# Why the widget is slow

- **Card:** Scout: widget speed
- **Project:** Alpha
- **Question:** Why does the widget take four seconds?
- **Verdict:** The poll waits on a lock the snapshot holds.
- **Confidence:** high
- **Recommendation:** build
- **Follow-up:** Split the lock — one reader, one writer
- **Sources:** widget.swift, daemon.py

## Question

Why?

## What was found

A lock.

## Evidence

A trace.

## Recommendation

Build it.

## Open questions

None.
"""

HEADERLESS = """# Notes without a block

Just prose, no answer block at all.
"""

MALFORMED = """# Half a report

- **Card:** Scout: half
- **Verdict:** Something

## Question

What?
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
    paths = {
        "complete": _write(a / "scout" / "2026-09-20-widget" / "report.md",
                           COMPLETE, 1_000_300),
        "headerless": _write(a / "scout" / "2026-09-21-notes" / "report.md",
                             HEADERLESS, 1_000_100),
        "malformed": _write(b / "scout" / "2026-09-22-half" / "report.md",
                            MALFORMED, 1_000_200),
        "undated": _write(a / "scout" / "misc" / "report.md", COMPLETE,
                          1_000_900),
    }
    return root_a, root_b, paths


def _build(roots, cards=()):
    return scout_index.build(roots, list(cards), refusal=REFUSAL)


def test_split_header_equals_parse_header_on_the_redis_report():
    report = REPO / "scout" / "2026-09-25-redis-command-volume" / "report.md"
    text = report.read_text() if report.is_file() else COMPLETE
    title, header, body = scout_index.split_header(text)
    assert header == scout_report.parse_header(text)
    assert header["verdict"]
    assert title and not body.startswith("# ")
    assert "- **Verdict:**" not in body
    assert body.lstrip().startswith("## Question")


def test_split_header_without_h1_or_block_is_the_text_whole():
    assert scout_index.split_header("no heading here\n") == (
        "", {}, "no heading here\n")
    assert scout_index.split_header(HEADERLESS) == ("", {}, HEADERLESS)


def test_rows_are_newest_first_by_mtime(tree):
    root_a, root_b, paths = tree
    index = _build([root_a, root_b])
    got = [r["path"] for r in index["rows"]]
    assert got == [paths["complete"], paths["malformed"], paths["headerless"]]
    assert index["roots"] == 2
    assert index["truncated"] is False and index["omitted"] == 0


def test_rows_carry_title_verdict_day_and_project(tree):
    root_a, _root_b, paths = tree
    row = _build([root_a])["rows"][0]
    assert row["path"] == paths["complete"]
    assert row["title"] == "Why the widget is slow"
    assert row["verdict"].startswith("The poll waits")
    assert row["confidence"] == "high"
    assert row["recommendation"] == "build"
    assert row["question"].startswith("Why does")
    assert row["day"] == "2026-09-20"
    assert row["folder"] == "2026-09-20-widget"
    assert row["project"] == "alpha"
    assert row["written_at"] == 1_000_300
    assert row["checked"] is True


def test_no_row_carries_text_or_body(tree):
    root_a, root_b, _paths = tree
    for row in _build([root_a, root_b])["rows"]:
        assert "text" not in row and "body" not in row
        assert "## What was found" not in repr(row)


def test_undated_folder_is_ignored(tree):
    root_a, _root_b, paths = tree
    got = [r["path"] for r in _build([root_a])["rows"]]
    assert paths["undated"] not in got


def test_malformed_report_is_listed_unchecked(tree):
    _root_a, root_b, paths = tree
    row = _build([root_b])["rows"][0]
    assert row["path"] == paths["malformed"]
    assert row["checked"] is False


def test_symlinked_report_and_fifo_are_skipped(tree, tmp_path):
    root_a, _root_b, paths = tree
    outside = tmp_path / "elsewhere.md"
    outside.write_text(COMPLETE)
    link_dir = Path(root_a) / "scout" / "2026-09-23-link"
    link_dir.mkdir(parents=True)
    os.symlink(outside, link_dir / "report.md")
    fifo_dir = Path(root_a) / "scout" / "2026-09-24-fifo"
    fifo_dir.mkdir(parents=True)
    os.mkfifo(fifo_dir / "report.md")
    got = [r["path"] for r in _build([root_a])["rows"]]
    assert got == [paths["complete"], paths["headerless"]]


def test_folders_per_root_bound_keeps_the_newest(tree, monkeypatch):
    root_a, _root_b, _paths = tree
    for day in range(1, 6):
        _write(Path(root_a) / "scout" / f"2026-10-0{day}-x" / "report.md",
               COMPLETE, 1_000_000 + day)
    monkeypatch.setattr(scout_index, "MAX_FOLDERS_PER_ROOT", 3)
    folders = [r["folder"] for r in _build([root_a])["rows"]]
    assert sorted(folders) == ["2026-10-03-x", "2026-10-04-x", "2026-10-05-x"]


def test_checked_is_none_past_the_head_bound(tree, monkeypatch):
    root_a, _root_b, _paths = tree
    monkeypatch.setattr(scout_index, "INDEX_HEAD_BYTES", 200)
    row = [r for r in _build([root_a])["rows"]
           if r["folder"] == "2026-09-20-widget"][0]
    assert row["checked"] is None
    assert row["title"] == "Why the widget is slow"


def test_a_cards_prose_report_outside_scout_is_listed(tree):
    root_a, _root_b, _paths = tree
    prose = _write(Path(root_a) / "docs" / "research" / "old.md",
                   "# An older prose report\n\nText.\n", 1_000_500)
    card = {"id": "c1", "title": "Scout: old", "column_name": "done",
            "root": root_a, "report_path": prose}
    rows = _build([root_a], [card])["rows"]
    assert rows[0]["path"] == prose
    assert rows[0]["title"] == "An older prose report"
    assert rows[0]["verdict"] == "" and rows[0]["day"] == ""
    assert rows[0]["card_id"] == "c1" and rows[0]["card_column"] == "done"


def test_a_card_on_a_scanned_report_annotates_rather_than_duplicates(tree):
    root_a, _root_b, paths = tree
    card = {"id": "c2", "title": "Scout: widget", "column_name": "in_progress",
            "root": root_a, "report_path": paths["complete"]}
    rows = _build([root_a], [card])["rows"]
    matching = [r for r in rows if r["path"] == paths["complete"]]
    assert len(matching) == 1
    assert matching[0]["card_title"] == "Scout: widget"
    assert len(rows) == 2


def test_a_card_under_an_unenrolled_root_is_not_listed(tree):
    root_a, root_b, paths = tree
    card = {"id": "c3", "title": "t", "column_name": "done",
            "root": root_b, "report_path": paths["malformed"]}
    rows = _build([root_a], [card])["rows"]
    assert paths["malformed"] not in [r["path"] for r in rows]


def test_locate_accepts_exactly_the_two_admitted_shapes(tree):
    root_a, root_b, paths = tree
    prose = _write(Path(root_a) / "docs" / "research" / "old.md",
                   "# Old\n", None)
    card = {"id": "c1", "root": root_a, "report_path": prose}
    roots = [root_a, root_b]
    resolved, root, why = scout_index.locate(
        paths["complete"], roots, [], refusal=REFUSAL)
    assert (resolved, root, why) == (paths["complete"], root_a, "")
    resolved, root, why = scout_index.locate(
        prose, roots, [card], refusal=REFUSAL)
    assert (resolved, root, why) == (prose, root_a, "")
    # The same prose file with no card pointing at it is not listed.
    assert scout_index.locate(prose, roots, [], refusal=REFUSAL)[2] == \
        scout_index.NOT_LISTED


def test_locate_refuses_outside_every_root(tree, tmp_path):
    root_a, root_b, _paths = tree
    stray = _write(tmp_path / "stray" / "scout" / "2026-09-20-x" / "report.md",
                   COMPLETE)
    for text in (stray, "/etc/passwd", "", "relative/report.md"):
        assert scout_index.locate(text, [root_a, root_b], [],
                                  refusal=REFUSAL) == (
            "", "", scout_index.NOT_LISTED)


def test_locate_refuses_a_path_under_an_unenrolled_root(tree):
    root_a, _root_b, paths = tree
    assert scout_index.locate(paths["malformed"], [root_a], [],
                              refusal=REFUSAL)[2] == scout_index.NOT_LISTED


def test_locate_refuses_an_undated_scout_folder(tree):
    root_a, _root_b, paths = tree
    assert scout_index.locate(paths["undated"], [root_a], [],
                              refusal=REFUSAL)[2] == scout_index.NOT_LISTED


def test_locate_refuses_a_symlink_resolving_out_of_the_root(tree, tmp_path):
    root_a, _root_b, _paths = tree
    outside = tmp_path / "outside.md"
    outside.write_text(COMPLETE)
    link_dir = Path(root_a) / "scout" / "2026-09-26-escape"
    link_dir.mkdir(parents=True)
    link = link_dir / "report.md"
    os.symlink(outside, link)
    assert scout_index.locate(str(link), [root_a], [],
                              refusal=REFUSAL)[2] == scout_index.NOT_LISTED


def test_read_splits_header_and_body(tree):
    root_a, _root_b, paths = tree
    got = scout_index.read(paths["complete"], [root_a], [], refusal=REFUSAL)
    assert got["available"] is True and got["reason"] == ""
    assert got["title"] == "Why the widget is slow"
    assert got["has_header"] is True and got["checked"] is True
    assert got["header"] == scout_report.parse_header(COMPLETE)
    assert got["body"].startswith("## Question")
    assert got["project"] == "alpha"
    assert got["written_at"] == 1_000_300


def test_read_on_a_headerless_report_returns_the_text_as_body(tree):
    root_a, _root_b, paths = tree
    got = scout_index.read(paths["headerless"], [root_a], [],
                           refusal=REFUSAL)
    assert got["available"] is True
    assert got["has_header"] is False
    assert got["header"] == {}
    assert got["body"] == HEADERLESS
    assert got["title"] == "Notes without a block"


def test_read_outside_the_set_is_unavailable_in_words(tree):
    root_a, _root_b, _paths = tree
    got = scout_index.read("/etc/hosts", [root_a], [], refusal=REFUSAL)
    assert got["available"] is False
    assert got["reason"] == scout_index.NOT_LISTED
    assert got["body"] == "" and got["header"] == {}


def test_read_of_an_empty_file_says_it_could_not_be_read(tree):
    root_a, _root_b, _paths = tree
    empty = _write(Path(root_a) / "scout" / "2026-09-27-empty" / "report.md",
                   "")
    got = scout_index.read(empty, [root_a], [], refusal=REFUSAL)
    assert got["available"] is False
    assert got["reason"] == scout_index.UNREADABLE


# ---- the text search over the bodies (`scout_index.search`) ----

def _search(roots, query, cards=()):
    return scout_index.search(roots, list(cards), query, refusal=REFUSAL)


def test_fold_casefolds_and_drops_combining_marks():
    assert scout_index.fold("Café") == "cafe"
    assert scout_index.fold("Zürich") == "zurich"
    assert scout_index.fold("MiXeD Case") == "mixed case"
    # NFKD leaves ł alone; this plan does not special-case it.
    assert scout_index.fold("łódź") == "łodz"
    assert scout_index.fold("") == ""


def test_search_finds_a_word_only_in_the_body_with_a_snippet(tree):
    root_a, root_b, paths = tree
    index = _search([root_a, root_b], "trace")
    assert [r["path"] for r in index["rows"]] == [paths["complete"]]
    hit = index["rows"][0]
    assert "trace" in hit["snippet"]
    assert hit["match_line"] >= 1
    assert hit["match"] == "body"
    assert "text" not in hit and "body" not in hit
    assert hit["verdict"] == "The poll waits on a lock the snapshot holds."
    assert index["query"] == "trace"
    assert index["searched"] == 3 and index["unsearched"] == 0
    assert index["search_truncated"] is False
    assert index["hits_truncated"] is False


def test_search_match_line_counts_body_lines(tree):
    root_a, _root_b, paths = tree
    hit = _search([root_a], "a trace")["rows"][0]
    _title, _header, body = scout_index.split_header(COMPLETE)
    assert body.splitlines()[hit["match_line"] - 1] == "A trace."


def test_a_word_only_in_the_answer_block_is_not_a_hit(tree):
    root_a, root_b, _paths = tree
    # "poll" is in the Verdict line alone; "four seconds" in the Question.
    assert _search([root_a, root_b], "poll")["rows"] == []
    assert _search([root_a, root_b], "four seconds")["rows"] == []


def test_search_ignores_case_and_accents(tmp_path):
    root = tmp_path / "gamma"
    root.mkdir()
    path = _write(root / "scout" / "2026-09-23-cafe" / "report.md",
                  "# Coffee\n\n- **Verdict:** fine\n\n## Evidence\n\n"
                  "The Café in Zürich serves it.\n", 1_000_000)
    base = os.path.realpath(root)
    for query in ("CAFE", "café", "zurich", "ZÜRICH", "  cafe in  "):
        rows = _search([base], query)["rows"]
        assert [r["path"] for r in rows] == [path], query
    assert "Café" in _search([base], "cafe")["rows"][0]["snippet"]


def test_a_query_out_of_bounds_is_refused_in_words(tree):
    root_a, _root_b, _paths = tree
    with pytest.raises(ValueError, match="at least 3 characters"):
        _search([root_a], "ab")
    with pytest.raises(ValueError, match="at least 3 characters"):
        _search([root_a], "  ab   ")
    with pytest.raises(ValueError, match="at most 200 characters"):
        _search([root_a], "x" * (scout_index.MAX_QUERY_CHARS + 1))
    assert _search([root_a], "x" * scout_index.MAX_QUERY_CHARS)["rows"] == []


def test_the_hit_cap_is_reported(tree, monkeypatch):
    root_a, root_b, paths = tree
    monkeypatch.setattr(scout_index, "SEARCH_MAX_HITS", 1)
    # "question" is a body heading in the complete and malformed reports.
    index = _search([root_a, root_b], "## question")
    assert [r["path"] for r in index["rows"]] == [paths["complete"]]
    assert index["hits_truncated"] is True


def test_the_hit_cap_is_not_claimed_when_nothing_more_matched(tree, monkeypatch):
    root_a, root_b, _paths = tree
    hits = _search([root_a, root_b], "## question")["rows"]
    assert len(hits) >= 1
    # A cap exactly as large as the matches is reached, never cut.
    monkeypatch.setattr(scout_index, "SEARCH_MAX_HITS", len(hits))
    index = _search([root_a, root_b], "## question")
    assert len(index["rows"]) == len(hits)
    assert index["hits_truncated"] is False


def test_the_reading_bound_is_reported(tree, monkeypatch):
    root_a, root_b, paths = tree
    monkeypatch.setattr(scout_index, "SEARCH_MAX_BODIES", 1)
    index = _search([root_a, root_b], "## question")
    assert index["searched"] == 1
    assert index["search_truncated"] is True
    assert [r["path"] for r in index["rows"]] == [paths["complete"]]


def test_an_oversized_report_is_skipped_not_clipped(tree, monkeypatch):
    root_a, _root_b, paths = tree
    size = os.path.getsize(paths["complete"])
    monkeypatch.setattr(scout_report, "MAX_REPORT_BYTES", size - 1)
    index = _search([root_a], "trace")
    assert index["rows"] == []
    assert index["unsearched"] == 1
    assert index["searched"] == 1  # the headerless report still fitted


def test_a_cards_prose_report_outside_scout_is_searched(tree):
    root_a, _root_b, _paths = tree
    prose = _write(Path(root_a) / "docs" / "research" / "old.md",
                   "# An older prose report\n\nThe relay spends commands.\n",
                   1_000_500)
    card = {"id": "c1", "title": "Scout: old", "column_name": "done",
            "root": root_a, "report_path": prose}
    rows = _search([root_a], "relay spends", [card])["rows"]
    assert [r["path"] for r in rows] == [prose]
    assert rows[0]["card_id"] == "c1"
    # No card, no row: the search reads only what the index lists.
    assert _search([root_a], "relay spends")["rows"] == []


def test_search_and_build_read_one_candidate_set(tree):
    root_a, root_b, paths = tree
    ordered, count = scout_index._candidates([root_a, root_b], [], REFUSAL)
    assert count == 2
    listed = [r["path"] for r in _build([root_a, root_b])["rows"]]
    assert [c["path"] for c in ordered] == listed
    assert paths["undated"] not in listed


def test_the_snippet_is_clipped_around_the_match():
    far = "word " * 80 + "needle" + " tail" * 80
    line_no, text = scout_index.snippet("first line\n" + far, "needle")
    assert line_no == 2
    assert "needle" in text
    assert len(text) <= scout_index.SNIPPET_CHARS
    assert text.startswith("…") and text.endswith("…")
    assert scout_index.snippet("nothing here", "needle") == (0, "")
    assert scout_index.snippet("a   spaced\tline", "spaced") == (
        1, "a spaced line")


def test_a_term_of_combining_marks_alone_is_refused(tree):
    root_a, _root_b, _paths = tree
    with pytest.raises(ValueError, match="at least 3 characters"):
        _search([root_a], "́́́")
    with pytest.raises(ValueError, match="at least 3 characters"):
        _search([root_a], "á́́")


def test_a_term_with_doubled_whitespace_still_carries_a_snippet(tree):
    root_a, _root_b, paths = tree
    for query in ("a  trace", "a\ttrace", " a \n trace "):
        index = _search([root_a], query)
        assert [r["path"] for r in index["rows"]] == [paths["complete"]], query
        hit = index["rows"][0]
        assert hit["snippet"] == "A trace." and hit["match_line"] >= 1, query
        assert index["query"] == "a trace"
