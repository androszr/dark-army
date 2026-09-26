# host/tests/test_scout_report.py
"""The scout report's parser and checker (`scout_report.py`) and its two
byte copies: the one this checkout's scouts run and the one the pack ships.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from dark_army_daemon import scout_report

REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "host" / "dark_army_daemon" / "scout_report.py"
LOCAL_COPY = REPO / ".claude" / "skills" / "scout" / "scout_check.py"
PACK_COPY = (REPO / "host" / "dark_army_menubar" / "agent_pack" / "template"
             / ".claude" / "skills" / "scout" / "scout_check.py")

HEADER = {
    "Card": "Scout: why X",
    "Project": "sample-app",
    "Question": "Why does X stall?",
    "Verdict": "X waits on a lock nobody releases.",
    "Confidence": "high",
    "Recommendation": "build",
    "Sources": "host/x.py, docs/x.md, the log",
}
FOLLOW_UPS = (
    "Release the lock on error — free X's lock in the failure path",
    "Log the holder — name who holds X's lock when it stalls",
)


def _report(header=None, follow_ups=FOLLOW_UPS, headings=scout_report.HEADINGS,
            drop=(), title="# Why X stalls") -> str:
    fields = dict(HEADER if header is None else header)
    lines = [title, ""]
    for key in scout_report.HEADER_KEYS:
        if key == "Follow-up":
            lines += [f"- **Follow-up:** {item}" for item in follow_ups]
            continue
        if key in drop or key not in fields:
            continue
        lines.append(f"- **{key}:** {fields[key]}")
    lines.append("")
    for heading in headings:
        lines += [f"## {heading}", "", "words", ""]
    return "\n".join(lines)


# --- parse ------------------------------------------------------------------


def test_a_complete_report_parses_to_the_expected_dict():
    assert scout_report.parse_header(_report()) == {
        "card": "Scout: why X",
        "project": "sample-app",
        "question": "Why does X stall?",
        "verdict": "X waits on a lock nobody releases.",
        "confidence": "high",
        "recommendation": "build",
        "sources": ["host/x.py", "docs/x.md", "the log"],
        "follow_ups": [
            {"title": "Release the lock on error",
             "summary": "free X's lock in the failure path"},
            {"title": "Log the holder",
             "summary": "name who holds X's lock when it stalls"},
        ],
    }


def test_a_complete_report_checks_clean():
    assert scout_report.check(_report()) == []


def test_a_report_with_no_follow_ups_and_no_sources_checks_clean():
    text = _report(header={**HEADER, "Sources": "none"}, follow_ups=())
    assert scout_report.check(text) == []
    parsed = scout_report.parse_header(text)
    assert "follow_ups" not in parsed and "sources" not in parsed


def test_the_block_ends_at_the_first_line_that_is_not_a_key():
    text = _report().replace("- **Verdict:**", "\n- **Verdict:**", 1)
    assert "verdict" not in scout_report.parse_header(text)
    assert "missing Verdict" in scout_report.check(text)


def test_a_report_with_no_title_has_no_block():
    text = _report().split("\n", 1)[1]
    assert scout_report.parse_header(text) == {}


# --- check ------------------------------------------------------------------


@pytest.mark.parametrize("key", scout_report.REQUIRED_KEYS)
def test_each_required_key_removed_yields_exactly_its_line(key):
    assert scout_report.check(_report(drop=(key,))) == [f"missing {key}"]


def test_follow_up_is_optional():
    assert "Follow-up" not in scout_report.REQUIRED_KEYS
    assert scout_report.check(_report(follow_ups=())) == []


def test_off_list_confidence_and_recommendation_yield_the_allowlist_lines():
    text = _report(header={**HEADER, "Confidence": "sure",
                           "Recommendation": "ship it"})
    assert scout_report.check(text) == [
        "confidence must be one of high, medium, low",
        "recommendation must be one of build, do-not-build, "
        "needs-decision, more-scouting",
    ]


def test_a_placeholder_verdict_reads_as_missing():
    text = _report(header={**HEADER, "Verdict": "<the answer, one line>"})
    assert scout_report.check(text) == ["missing Verdict"]
    assert "verdict" not in scout_report.parse_header(text)


def test_a_none_verdict_is_empty():
    text = _report(header={**HEADER, "Verdict": "none"})
    assert scout_report.check(text) == ["verdict is empty"]


def test_a_follow_up_without_the_dash_is_named():
    text = _report(follow_ups=("Just a title",))
    assert scout_report.check(text) == [
        'follow-up line needs "<title> — <summary>"']


@pytest.mark.parametrize("line", [
    "Fix X — <one-line summary>",
    "<card title> — free the lock",
    "<card title> — <one-line summary>",
    "<card title>",
])
def test_a_follow_up_with_a_placeholder_half_is_named(line):
    assert scout_report.check(_report(follow_ups=(line,))) == [
        'follow-up line needs "<title> — <summary>"']


def test_shuffled_headings_are_out_of_order():
    order = ("Question", "Evidence", "What was found", "Recommendation",
             "Open questions")
    problems = scout_report.check(_report(headings=order))
    assert problems
    assert all("out of order" in p for p in problems), problems


def test_a_dropped_heading_is_missing():
    headings = tuple(h for h in scout_report.HEADINGS if h != "Evidence")
    assert scout_report.check(_report(headings=headings)) == [
        'heading "## Evidence" missing']


def test_a_heading_inside_a_fence_does_not_count():
    text = _report(headings=scout_report.HEADINGS[:-1]) + \
        "\n```\n## Open questions\n```\n"
    assert scout_report.check(text) == ['heading "## Open questions" missing']


def test_a_41_line_header_yields_the_length_line():
    extra = scout_report.MAX_HEADER_LINES + 1 - len(scout_report.REQUIRED_KEYS)
    follow_ups = tuple(f"Card {i} — does thing {i}" for i in range(extra))
    text = _report(follow_ups=follow_ups)
    assert scout_report.check(text) == ["header longer than 40 lines"]
    at_bound = _report(follow_ups=follow_ups[:-1])
    assert scout_report.check(at_bound) == []


def test_brief_folds_the_missing_keys_into_one_phrase():
    problems = scout_report.check("# T\n\n## Question\n")
    line = scout_report.brief(problems)
    assert line.startswith("missing Card, Project, Question, Verdict")
    assert "; and " in line and line.endswith(" more")


# --- read_header ------------------------------------------------------------


def test_read_header_reads_a_report(tmp_path):
    path = tmp_path / "report.md"
    path.write_text(_report(), encoding="utf-8")
    assert scout_report.read_header(path)["verdict"] == HEADER["Verdict"]


def test_read_header_over_the_bound_is_empty(tmp_path):
    path = tmp_path / "report.md"
    path.write_text(_report() + "x" * (65 * 1024), encoding="utf-8")
    assert scout_report.read_header(path) == {}


def test_read_header_on_a_missing_path_is_empty(tmp_path):
    assert scout_report.read_header(tmp_path / "nope.md") == {}


# --- main, through the checkout's copy ----------------------------------------


def _run(path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(LOCAL_COPY), str(path)],
                          capture_output=True, text=True, timeout=30)


def test_main_prints_ok_and_exits_0(tmp_path):
    path = tmp_path / "report.md"
    path.write_text(_report(), encoding="utf-8")
    done = _run(path)
    assert done.returncode == 0
    assert done.stdout == "ok\n"


def test_main_prints_each_problem_and_exits_1(tmp_path):
    path = tmp_path / "report.md"
    path.write_text(_report(drop=("Verdict",), headings=()), encoding="utf-8")
    done = _run(path)
    assert done.returncode == 1
    lines = done.stdout.splitlines()
    assert lines[0] == "missing Verdict"
    assert len(lines) == 1 + len(scout_report.HEADINGS)


def test_main_on_a_missing_file_exits_1(tmp_path):
    done = _run(tmp_path / "nope.md")
    assert done.returncode == 1
    assert "cannot read" in done.stdout


# --- the three copies -----------------------------------------------------------


def test_the_three_copies_are_byte_identical():
    source = MODULE.read_bytes()
    assert LOCAL_COPY.read_bytes() == source
    assert PACK_COPY.read_bytes() == source


def test_it_parses_under_python_3_9_and_imports_only_the_stdlib():
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 9))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import reaches the daemon"
            names.add((node.module or "").split(".")[0])
    assert names <= set(sys.stdlib_module_names), names
    assert not any(n.startswith("dark_army") for n in names)


def test_it_carries_no_placeholder_braces():
    assert "{{" not in MODULE.read_text(encoding="utf-8")


def test_read_text_refuses_a_link_and_a_fifo(tmp_path):
    """The daemon hands a realpath; a final symlink or a FIFO reads nothing,
    and neither blocks."""
    import os
    real = tmp_path / "report.md"
    real.write_text(_report(), encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(real)
    fifo = tmp_path / "fifo.md"
    os.mkfifo(fifo)
    assert scout_report.read_text(real) == _report()
    assert scout_report.read_text(link) == ""
    assert scout_report.read_text(fifo) == ""
    assert scout_report.read_header(link) == {}

