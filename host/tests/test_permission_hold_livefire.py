"""The live-fire check's *pure* parts, and only those.

`tools/permission_hold_livefire.py` is the command that settles whether the
CLI's own permission dialog stays drawn and answerable while Dark Army's hook broker
holds it. The answer belongs to the installed CLI, not to Dark Army, so it can only
be had by running the thing against a real daemon and a real session — and
**this file is not that run and must never be read as standing in for it**.
What it covers is the arithmetic around the observation: the screen
classifier, the verdict table, and the record's required lines. Those are the
parts that could quietly start lying without anybody noticing, because the
live run prints whatever they say.

The paints are fixtures recorded from a real run and checked in beside this
file, so the classifier is tested against what the emulator actually handed
over — escape sequences, box drawing and all — rather than against a
hand-written approximation of it.
"""

import importlib.util
from pathlib import Path

import pytest

_TOOL = (Path(__file__).resolve().parents[2] / "tools"
         / "permission_hold_livefire.py")
_PAINTS = Path(__file__).resolve().parent / "data" / "permission_hold_livefire"


def _load():
    """Import the script by path: it lives under `tools/`, which is not a
    package and is not on `sys.path`."""
    spec = importlib.util.spec_from_file_location(
        "permission_hold_livefire", _TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lf = _load()


def _paint(name: str) -> bytes:
    return (_PAINTS / name).read_bytes()


def test_the_script_is_stdlib_only():
    """It runs from a shell against whatever python is to hand and imports
    nothing from `host/`. A package import here would make the check
    un-runnable on the machine that most needs it."""
    third_party = {"rumps", "psutil", "objc", "requests", "yaml", "pytest"}
    for line in _TOOL.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not (stripped.startswith("import ") or stripped.startswith("from ")):
            continue
        name = stripped.split()[1].split(".")[0]
        assert name not in third_party, line
        assert not name.startswith("bob_companion"), line


# --- the screen classifier -------------------------------------------------

def test_a_real_dialog_paint_reads_as_a_dialog():
    """The recorded paint of the CLI's own permission dialog, taken through
    the terminal stream while the broker was holding it."""
    assert lf.classify_paint(_paint("dialog-paint.txt"), "Bash") == lf.DIALOG


def test_a_plain_prompt_paint_reads_as_no_dialog():
    """A readable screen with no dialog on it is the *informative* failure —
    it is what `AWAITED` is made of — so it must never be confused with a
    screen nobody could read."""
    assert lf.classify_paint(_paint("plain-paint.txt"), "Bash") == lf.NO_DIALOG


@pytest.mark.parametrize("paint", [b"", "", "   \n\t ", None])
def test_nothing_to_read_is_unreadable_not_no_dialog(paint):
    assert lf.classify_paint(paint, "Bash") == lf.UNREADABLE


def test_a_paint_carrying_only_escape_sequences_is_unreadable():
    assert lf.classify_paint("\x1b[2J\x1b[H\x1b[0m", "Bash") == lf.UNREADABLE


def test_a_dialog_for_another_tool_is_not_this_asks_dialog():
    """The furniture alone is not enough: a dialog about some other tool is
    not the one the daemon published, and counting it would let a leftover
    screen answer for an ask that was never drawn."""
    assert lf.classify_paint(_paint("dialog-paint.txt"), "WebFetch") \
        == lf.NO_DIALOG


def test_a_transcript_merely_mentioning_the_tool_is_not_a_dialog():
    assert lf.classify_paint(
        "> please run Bash for me\n\nSure, running it now.\n", "Bash") \
        == lf.NO_DIALOG


def test_the_markers_survive_a_line_wrap():
    """A dialog's words are routinely split across a wrap or a colour change,
    so matching happens on the flattened text. A classifier that matched the
    raw bytes would read a real dialog as absent on a narrow terminal."""
    wrapped = ("Bash command\n\x1b[38;5;250mDo you want to\x1b[0m\n"
               "\x1b[38;5;250mproceed?\x1b[0m\n")
    assert lf.classify_paint(wrapped, "Bash") == lf.DIALOG


def test_strip_ansi_drops_osc_titles_as_well_as_csi():
    """The pty carries Dark Army's own OSC 0 title write; left in, it would put the
    session's nickname into the text the markers are matched against."""
    flat = lf.strip_ansi("\x1b]0;Claude Code · Bash\x07hello \x1b[1mworld\x1b[0m")
    assert flat == "hello world"


# --- the permission mode, and who answered ---------------------------------

def test_a_mode_that_answers_for_itself_is_named():
    """What decides whether there is an ask to watch at all is the session's
    permission mode, not the command: under `defaultMode: auto` the CLI ran
    both `rm -f` and an outbound `curl` with no dialog and no
    `PermissionRequest`. The footer strings are the ones the 2.1.263 run
    banked in the record showed."""
    assert lf.permission_mode_marker(
        "Context: 90% remaining\n\x1b[2m⏵⏵ auto mode on (shift+tab to "
        "cycle)\x1b[0m") == "auto mode on"


def test_the_cli_default_mode_names_no_mode():
    """The stop condition for the cycling. A fixed number of Shift-Tab
    presses would silently stop meaning "default" the first time the CLI adds
    a mode to the cycle."""
    assert lf.permission_mode_marker(
        "Context: 90% remaining\n⏸ manual mode on") == ""
    assert lf.permission_mode_marker("") == ""


def test_the_recorded_dialog_paint_credits_nobody():
    """A dialog that is up has been answered by neither side."""
    assert lf.hook_credit_count(_paint("dialog-paint.txt")) == 0


def test_the_recorded_paint_after_bobs_allow_credits_the_hook():
    """`Allowed by PermissionRequest hook` is the CLI naming which of the two
    concurrent answers it used — the finding leg B1 turns on."""
    assert lf.hook_credit_count(_paint("plain-paint.txt")) == 1
    assert lf.bob_answer_landed(_paint("plain-paint.txt")) is True


def test_the_credit_is_counted_not_flagged():
    """Leg B2 runs with leg B1's credit line already on the screen above it,
    so "did a hook answer *this* one" is a count that must not move, never a
    flag that is already set."""
    screen = ("⎿ Allowed by PermissionRequest hook\n⎿ $ x\n"
              "⎿ Denied by PermissionRequest hook\n")
    assert lf.hook_credit_count(screen) == 2


def test_a_selection_that_moved_is_a_different_picture():
    """Leg A's second half: one cursor-down, and the two recorded paints of
    the same dialog must not compare equal, or a frozen dialog would read as
    an answerable one."""
    first = _paint("dialog-paint.txt")
    second = _paint("dialog-paint-selection-moved.txt")
    assert lf.classify_paint(first, "Bash") == lf.DIALOG
    assert lf.classify_paint(second, "Bash") == lf.DIALOG
    assert lf._differs(first, second) is True
    assert lf._differs(first, first) is False
    assert lf._differs(first, b"") is False


# --- the verdict table -----------------------------------------------------

_PAINT_CLASSES = (lf.DIALOG, lf.NO_DIALOG, lf.UNREADABLE)
_ANSWERABLE = (lf.MOVED, lf.FROZEN, lf.UNSEEN)


@pytest.mark.parametrize("paint_class", _PAINT_CLASSES)
@pytest.mark.parametrize("answerable", _ANSWERABLE)
def test_the_verdict_table_over_all_nine_leg_a_combinations(paint_class,
                                                            answerable):
    """The nine shapes leg A can come back in, with both B sub-runs passing.
    Only one of them may raise a constant."""
    got = lf.verdict_for(paint_class, answerable, lf.PASS, lf.PASS)
    if paint_class == lf.NO_DIALOG:
        assert got == lf.AWAITED
    elif paint_class == lf.DIALOG and answerable == lf.MOVED:
        assert got == lf.CONCURRENT
    else:
        assert got == lf.INCONCLUSIVE


def test_no_dialog_and_unreadable_do_not_collapse_to_one_answer():
    """The two failure shapes are opposite. `NO_DIALOG` says the CLI ran the
    hooks first — a real finding. `UNREADABLE` says the check could not see.
    Reading a broken socket as proof the dialog was never drawn is the way
    this check would talk somebody out of a feature that works."""
    awaited = lf.verdict_for(lf.NO_DIALOG, lf.UNSEEN, lf.SKIPPED, lf.SKIPPED)
    unreadable = lf.verdict_for(lf.UNREADABLE, lf.UNSEEN, lf.SKIPPED,
                                lf.SKIPPED)
    assert awaited == lf.AWAITED
    assert unreadable == lf.INCONCLUSIVE
    assert awaited != unreadable


@pytest.mark.parametrize("b1,b2", [
    (lf.PASS, lf.FAIL), (lf.FAIL, lf.PASS), (lf.FAIL, lf.FAIL),
    (lf.PASS, lf.SKIPPED), (lf.SKIPPED, lf.PASS), (lf.SKIPPED, lf.SKIPPED),
])
def test_a_disagreeing_race_is_never_concurrent(b1, b2):
    """`CONCURRENT` is the only verdict that moves a constant, so it needs
    every leg. Half a race proves half of nothing."""
    assert lf.verdict_for(lf.DIALOG, lf.MOVED, b1, b2) == lf.INCONCLUSIVE


def test_a_frozen_dialog_is_inconclusive_not_concurrent():
    """A dialog drawn but not taking keys would mean Dark Army's answer is the only
    one available — the opposite of the race the safety case rests on."""
    assert lf.verdict_for(lf.DIALOG, lf.FROZEN, lf.PASS, lf.PASS) \
        == lf.INCONCLUSIVE


def test_the_verdict_is_always_one_of_the_three():
    for paint_class in _PAINT_CLASSES:
        for answerable in _ANSWERABLE:
            for b1 in (lf.PASS, lf.FAIL, lf.SKIPPED):
                for b2 in (lf.PASS, lf.FAIL, lf.SKIPPED):
                    assert lf.verdict_for(paint_class, answerable, b1, b2) \
                        in (lf.CONCURRENT, lf.AWAITED, lf.INCONCLUSIVE)


# --- the record ------------------------------------------------------------

RECORD = (Path(__file__).resolve().parents[2] / "docs"
          / "2026-09-07-permission-hold-verification.md")


def test_the_record_exists_and_carries_every_required_line():
    """Five lines, each at the start of a line of its own, so a later reader
    — or a script on a CLI bump — finds the answer without reading prose."""
    text = RECORD.read_text(encoding="utf-8")
    assert lf.missing_record_lines(text) == []


def test_the_records_verdict_is_one_the_script_can_emit():
    text = RECORD.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("VERDICT:"))
    assert line.split(":", 1)[1].strip() in (
        lf.CONCURRENT, lf.AWAITED, lf.INCONCLUSIVE)


def test_the_record_never_claims_the_subagent_path():
    """The CLI awaits the hooks in the async-subagent spawn context and the
    daemon refuses to hold such an ask. Whatever the verdict says, the record
    has to say out loud that it does not speak for that path."""
    text = RECORD.read_text(encoding="utf-8")
    scope = next(ln for ln in text.splitlines() if ln.startswith("SCOPE:"))
    assert "subagent" in scope


def test_the_record_names_an_exact_cli_version():
    import re
    text = RECORD.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("CLI-VERSION:"))
    assert re.search(r"\b\d+\.\d+\.\d+\b", line), line


def test_the_four_machine_findable_lines_appear_exactly_once_each():
    """The check's stdout is banked verbatim, and the summary lines are
    lifted out of the fenced block to the document's top level rather than
    left inside it — so a grep for the verdict finds one answer, not two."""
    text = RECORD.read_text(encoding="utf-8")
    for want in ("VERDICT:", "SCOPE:", "CLI-VERSION:", "HOLD-CEILING:"):
        hits = [ln for ln in text.splitlines() if ln.startswith(want)]
        assert len(hits) == 1, (want, hits)


def test_the_hold_ceiling_is_stated_either_way():
    """`n/a` is a real answer — the ceiling is only measurable once the
    verdict is `CONCURRENT`. What is not allowed is the line being absent, or
    a measured ceiling sitting under a verdict that never earned one."""
    text = RECORD.read_text(encoding="utf-8")
    verdict = next(ln for ln in text.splitlines()
                   if ln.startswith("VERDICT:")).split(":", 1)[1].strip()
    ceiling = next(ln for ln in text.splitlines()
                   if ln.startswith("HOLD-CEILING:")).split(":", 1)[1].strip()
    assert ceiling
    if verdict != lf.CONCURRENT:
        assert ceiling == "n/a", ceiling


def test_read_token_refuses_without_the_desk_token(monkeypatch):
    """Every verb the livefire sends is a desk verb, so it reads the desk
    token from the environment and never the on-disk session token — and
    says where to get it when it is missing."""
    monkeypatch.delenv("DARK_ARMY_DESK_TOKEN", raising=False)
    with pytest.raises(lf.Refusal) as err:
        lf.read_token()
    assert "Copy desk key" in str(err.value)
    assert "DARK_ARMY_DESK_TOKEN" in str(err.value)
    assert not hasattr(lf, "TOKEN_PATH")


def test_read_token_returns_the_desk_token_from_the_environment(monkeypatch):
    monkeypatch.setenv("DARK_ARMY_DESK_TOKEN", "  desk-value\n")
    assert lf.read_token() == "desk-value"
