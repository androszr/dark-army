"""The Checks search and the Reports search fold one way.

The same stored text and query go through `manual_check.matches` and
`scout_index.search`; both must give the same answer. Real files on a
`tmp_path` root, no daemon, no HTTP.
"""

import os

import pytest

from dark_army_daemon import manual_check, scout_index
from dark_army_daemon.daemon_board import BoardVerbsMixin

REFUSAL = BoardVerbsMixin._plan_path_refusal

CHECK = """# {text}

- **Card:** Fold card
- **Project:** dark-army
- **Check:** the fold
- **Created:** 2026-10-03T10:00:00+02:00
- **Status:** open
- **Outcome:** none
- **Checked at:** none

## Steps

1. Look.

## Why not automated

A screen.
"""

REPORT = """# Fold report

- **Card:** Scout: fold
- **Project:** Alpha
- **Question:** Does it fold?
- **Verdict:** Yes.
- **Confidence:** high
- **Recommendation:** build
- **Follow-up:** None
- **Sources:** none

## Question

Does it?

## What was found

{text}

## Evidence

None.
"""

CASES = [
    ("Café menu", "cafe", True),
    ("cafe menu", "CAFÉ", True),
    ("Zürich office", "ZURICH", True),
    ("Mixed Case Word", "mixed case", True),
    ("naïve approach", "naive", True),
    ("łódź trip", "lodz", False),
    ("plain words", "absent", False),
]


def _checks_hit(root, text, query):
    folder = root / "manual-check" / "2026-10-03-fold"
    folder.mkdir(parents=True)
    (folder / "check.md").write_text(CHECK.format(text=text))
    entries = manual_check.scan(root)
    assert entries
    return any(manual_check.matches(e, query) for e in entries)


def _reports_hit(root, text, query):
    folder = root / "scout" / "2026-10-03-fold"
    folder.mkdir(parents=True)
    path = folder / "report.md"
    path.write_text(REPORT.format(text=text))
    real = os.path.realpath(path)
    result = scout_index.search([os.path.realpath(root)], [], query,
                                refusal=REFUSAL)
    return any(row.get("path") == real for row in result["rows"])


@pytest.mark.parametrize("text,query,expected", CASES)
def test_both_searches_give_the_same_answer(tmp_path, text, query, expected):
    assert len(query.strip()) >= scout_index.MIN_QUERY_CHARS
    checks_root = tmp_path / "checks"
    reports_root = tmp_path / "reports"
    checks_root.mkdir()
    reports_root.mkdir()
    checks = _checks_hit(checks_root, text, query)
    reports = _reports_hit(reports_root, text, query)
    assert checks == reports == expected


def test_a_query_of_combining_marks_alone_is_rejected_by_both():
    marks = "́́́"
    assert manual_check.matches({"title": "Café"}, marks) is False
    with pytest.raises(ValueError):
        scout_index.search([], [], marks, refusal=REFUSAL)
