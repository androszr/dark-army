# host/tests/test_review_run.py
"""The Review section's pure half: the parsers, the scope sentence, the
prompt and the Continue line, the records.

`review_run` is stdlib plus `paths`; nothing here opens a pty or runs git.
"""

import json

import pytest

from dark_army_daemon import dispatch, review_run
from dark_army_menubar import review_steps

REPORT = """\
VERDICT: STOP — the save drops a row

BLOCK · the save drops a row when two people edit at once
  Why it matters: a person loses their edit without being told
  Where:          host/dark_army_daemon/board.py:120
  Fix:            write the row inside the same transaction
  Confidence:     confirmed

FIX · the error message names no file
  Why it matters: nobody can tell which file failed
  Where:          host/dark_army_daemon/paths.py:44
  Fix:            say the file name
  Confidence:     likely

WARN · a retry is silent
  Why it matters: a person cannot tell it happened
  Fix:            log the retry

NOTE · the comment is out of date
  Where:          docs/context-host.md:9
"""


def test_every_grade_parses_in_order_with_its_fields():
    parsed = review_run.parse_findings(REPORT)
    assert parsed["verdict"] == "STOP"
    assert [f["grade"] for f in parsed["findings"]] == [
        "BLOCK", "FIX", "WARN", "NOTE"]
    assert [f["index"] for f in parsed["findings"]] == [1, 2, 3, 4]
    first = parsed["findings"][0]
    assert first["line"] == "the save drops a row when two people edit at once"
    assert first["where"] == "host/dark_army_daemon/board.py:120"
    assert first["fix"] == "write the row inside the same transaction"
    assert first["confidence"] == "confirmed"
    assert parsed["truncated"] is False


def test_a_stop_verdict_and_a_ship_verdict():
    assert review_run.parse_findings("VERDICT: SHIP\nnothing worth acting on")[
        "verdict"] == "SHIP"
    assert review_run.parse_findings(REPORT)["verdict"] == "STOP"


def test_a_finding_with_no_where_has_an_empty_one():
    third = review_run.parse_findings(REPORT)["findings"][2]
    assert third["grade"] == "WARN"
    assert third["where"] == ""
    assert third["fix"] == "log the retry"


def test_no_verdict_line_is_not_a_report():
    assert review_run.parse_findings("FIX · something\n") is None
    assert review_run.parse_findings("") is None


def test_long_lines_are_clamped():
    text = "VERDICT: SHIP\n\nFIX · " + "x" * 900 + "\n  Fix: " + "y" * 900 + "\n"
    finding = review_run.parse_findings(text)["findings"][0]
    assert len(finding["line"]) <= review_run.MAX_LINE_CHARS
    assert len(finding["fix"]) <= review_run.MAX_FIX_CHARS


def test_findings_past_the_bound_say_truncated():
    text = "VERDICT: SHIP\n" + "".join(
        f"NOTE · finding {i}\n  Where: f.py:{i}\n" for i in range(review_run.MAX_FINDINGS + 5))
    parsed = review_run.parse_findings(text)
    assert len(parsed["findings"]) == review_run.MAX_FINDINGS
    assert parsed["truncated"] is True


def test_a_started_step_then_done_keeps_the_last_line():
    parsed = review_run.parse_steps(
        "STEP commit: started — committing\nSTEP commit: done — abc123\n"
        "STEP push: failed — no upstream\nDONE\n")
    assert parsed["lines"] == [
        {"id": "commit", "status": "done", "words": "abc123"},
        {"id": "push", "status": "failed", "words": "no upstream"}]
    assert parsed["done"] is True


def test_no_done_line_means_not_done():
    parsed = review_run.parse_steps("STEP commit: done — ok\n")
    assert parsed["done"] is False
    assert review_run.parse_steps("")["lines"] == []


def test_steps_for_own_checkout_offers_the_five():
    steps = review_run.steps_for(own=True, after_steps=[], has_upstream=True)
    assert [s["id"] for s in steps] == [
        "rebuild", "restart", "commit", "push", "testflight"]
    assert all(s["how"] for s in steps)


def test_steps_for_a_profile_uses_its_list():
    steps = review_run.steps_for(
        own=False, has_upstream=True,
        after_steps=[["lint", "Lint", "run pnpm lint"],
                     ["commit", "Commit", "commit it"]])
    assert [s["id"] for s in steps] == ["lint", "commit"]


def test_steps_for_a_project_with_no_profile_is_commit_and_push():
    steps = review_run.steps_for(own=False, after_steps=[], has_upstream=True)
    assert [s["id"] for s in steps] == ["commit", "push"]


def test_push_is_dropped_without_an_upstream():
    steps = review_run.steps_for(own=True, after_steps=[], has_upstream=False)
    assert "push" not in [s["id"] for s in steps]
    steps = review_run.steps_for(own=False, after_steps=[], has_upstream=False)
    assert [s["id"] for s in steps] == ["commit"]


def test_a_malformed_profile_step_is_ignored():
    steps = review_run.steps_for(
        own=False, has_upstream=True,
        after_steps=[["Bad Id", "x", "y"], "nope", ["ok", "Ok", "do it"]])
    assert [s["id"] for s in steps] == ["ok"]


def test_scope_for_the_two_sentences():
    scope, line = review_run.scope_for("origin/main", 3, 5)
    assert scope == "remote"
    assert line == "everything not on the remote branch, 3 commits ahead and 5 changed files"
    assert review_run.scope_for("origin/main", 1, 1)[1].endswith(
        "1 commit ahead and 1 changed file")
    scope, line = review_run.scope_for("", 0, 2)
    assert scope == "working-tree"
    assert line == "no remote branch, uncommitted changes only"


def _run(steps=()):
    return review_run.new_record(
        run_id="ab12cd34", root="/p", project="p", tool="claude",
        scope="remote", scope_line="everything not on the remote branch, "
        "1 commit ahead and 2 changed files", upstream="origin/main",
        steps=list(steps), now=1.0)


def test_the_prompt_leads_with_the_slash_command_and_passes_the_guard():
    steps = review_run.steps_for(own=True, after_steps=[], has_upstream=True)
    run = _run([s for s in steps if s["id"] in ("commit", "push")])
    text = review_run.prompt(run)
    assert text.startswith("/review remote")
    assert "Dark Army review run:" in text
    assert "~/.dark-army/review-runs/ab12cd34/" in text
    assert dispatch.prompt_refusal("claude", text) is None
    assert dispatch.prompt_refusal("codex", text) is None


def test_the_prompt_lists_only_the_ticked_steps_and_denies_the_rest():
    steps = review_run.steps_for(own=True, after_steps=[], has_upstream=True)
    run = _run([s for s in steps if s["id"] == "commit"])
    text = review_run.prompt(run)
    assert "1. commit" in text
    assert "2. " not in text
    assert "Not authorised in this run: push, rebuild, restart, testflight." in text
    assert "any step not listed here is not authorised" in text.lower()
    assert "Never signal the pty broker" in text


def test_the_prompt_with_no_steps_authorises_nothing():
    text = review_run.prompt(_run())
    assert "No after-steps are authorised." in text
    assert "Not authorised in this run: commit, push" in text


def test_the_continue_line_is_one_short_printable_line():
    line = review_run.continue_line(_run([{"id": "commit", "label": "c", "how": "h"}]),
                                    [2, 3])
    assert "\n" not in line and "\r" not in line
    assert all(32 <= ord(c) < 127 for c in line)
    assert len(line) < 400
    assert "picks.json" in line and "no others" in line


def test_records_round_trip_keeping_an_unknown_key_and_defaulting_a_missing_one(tmp_path):
    path = tmp_path / "runs.json"
    rec = _run()
    rec["from_the_future"] = {"k": 1}
    review_run.save_records(path, [rec])
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    loaded = review_run.load_records(path)
    assert loaded[0]["from_the_future"] == {"k": 1}
    path.write_text(json.dumps({"version": 1, "runs": [{"id": "ee11ee11"}]}))
    sparse = review_run.load_records(path)[0]
    assert sparse["state"] == "reviewing" and sparse["findings"] == []
    assert sparse["handle"] == "" and sparse["picks"] == []


def test_a_corrupt_or_missing_file_reads_as_no_runs(tmp_path):
    assert review_run.load_records(tmp_path / "nope.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert review_run.load_records(bad) == []


def test_prune_drops_old_finished_runs_and_keeps_live_ones():
    old = _run(); old.update(id="00000001", state="done", finished_at=1.0)
    live = _run(); live.update(id="00000002", state="fixing")
    fresh = _run(); fresh.update(id="00000003", state="done", finished_at=1000.0)
    kept, dropped = review_run.prune(
        [old, live, fresh], 1000.0 + review_run.KEEP_FINISHED_SECONDS - 5)
    assert [r["id"] for r in dropped] == ["00000001"]
    assert {r["id"] for r in kept} == {"00000002", "00000003"}


def test_generic_steps_are_a_byte_equal_twin_of_the_menubar_copy():
    assert tuple(review_run.GENERIC_STEPS) == tuple(review_steps.GENERIC_STEPS)


def test_the_pack_shape_check():
    assert review_steps.check_steps(None)[0][0] == "commit"
    with pytest.raises(ValueError):
        review_steps.check_steps([["a", "A", "x"], ["a", "A", "y"]])
    with pytest.raises(ValueError):
        review_steps.check_steps([["Up", "A", "x"]])
    with pytest.raises(ValueError):
        review_steps.check_steps([["ok", "A", " "]])


STEP6_REPORT = """\
VERDICT: STOP — the save drops a row

BLOCK · the save drops a row when two people edit at once
  Why it matters: a person loses their edit
  Where:          board.py:120
  Fix:            write the row inside the transaction
  Confidence:     confirmed

FIX · the error message names no file
  Why it matters: nobody can tell which file failed
  Where:          paths.py:44
  Fix:            say the file name
  Confidence:     likely

WARN · a retry is silent
  Why it matters: nobody can tell it happened
  Where:          client.py:9
  Fix:            log the retry
  Confidence:     likely

## Do next

- BLOCK: the save drops a row when two people edit at once
- FIX · the error message names no file
- FIX: a summary line with a different wording
"""


def test_the_do_next_list_is_not_findings_and_numbering_holds():
    parsed = review_run.parse_findings(STEP6_REPORT)
    assert [f["index"] for f in parsed["findings"]] == [1, 2, 3]
    assert [f["grade"] for f in parsed["findings"]] == ["BLOCK", "FIX", "WARN"]


def test_a_repeated_grade_line_without_a_heading_does_not_shift_numbers():
    text = ("VERDICT: SHIP\n\nFIX · a\n  Where: a.py:1\n\nWARN · b\n  Where: b.py:1\n"
            "\nFIX · a\n\nNOTE · c\n  Where: c.py:1\n")
    parsed = review_run.parse_findings(text)
    assert [(f["index"], f["line"]) for f in parsed["findings"]] == [
        (1, "a"), (2, "b"), (3, "c")]


def test_the_prompt_and_the_continue_line_always_ask_for_done():
    for steps in ([], [{"id": "commit", "label": "c", "how": "h"}]):
        run = _run(steps)
        assert "always write `DONE` as the last line" in review_run.prompt(run)
        assert "DONE" in review_run.continue_line(run, [1])
        assert "match on the grade and line text" in review_run.continue_line(run, [1])
        assert "no others" in review_run.continue_line(run, [1])
    assert "match the picks on that text" in review_run.prompt(_run())


def test_a_finding_after_the_do_next_heading_is_kept():
    text = ("VERDICT: STOP\n\nFIX · a\n  Where: a.py:1\n\n## Do next\n\n- FIX: a\n"
            "\nWARN · b\n  Where: b.py:1\n  Fix: do b\n")
    parsed = review_run.parse_findings(text)
    assert [(f["index"], f["grade"], f["where"]) for f in parsed["findings"]] == [
        (1, "FIX", "a.py:1"), (2, "WARN", "b.py:1")]


def test_summary_bullets_before_the_full_blocks_keep_numbering_and_fields():
    text = ("VERDICT: STOP\n\n- BLOCK: one\n- FIX: two\n\nBLOCK · one\n"
            "  Where: a.py:1\n  Fix: fix one\n\nFIX · two\n  Where: b.py:2\n"
            "  Fix: fix two\n")
    parsed = review_run.parse_findings(text)
    assert [(f["index"], f["grade"], f["line"], f["fix"]) for f in parsed["findings"]] == [
        (1, "BLOCK", "one", "fix one"), (2, "FIX", "two", "fix two")]
