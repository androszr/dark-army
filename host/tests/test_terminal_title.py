"""The agent's name on its terminal tab."""
import os

import pytest

from dark_army_daemon import terminal_title as tt
from dark_army_daemon.identity import NAMES


class FakeTerminal:
    """A tty that records what was written at it, standing in for /dev/ttys004."""

    def __init__(self):
        self.writes: list[tuple[str, str]] = []
        self.refuse: set[str] = set()

    def writer(self, tty, title):
        if tty in self.refuse:
            return False
        self.writes.append((tty, title))
        return True

    @property
    def titles(self):
        return [title for _tty, title in self.writes]


def _writer(ttys: dict, terminal: FakeTerminal, enabled=True):
    w = tt.TitleWriter(enabled=enabled, resolver=lambda pid: ttys.get(pid, ""))
    w._write = terminal.writer
    return w


def _row(sid="s1", pid=101, nickname="Vex", name="Rewrite the pipeline",
         **extra):
    row = {"session_id": sid, "pid": pid, "nickname": nickname, "name": name,
           "provider": "claude", "idle_seconds": 1.0, "project": "dark-army"}
    row.update(extra)
    return row


# ── the badge ────────────────────────────────────────────────────────────────

def test_short_is_three_letters():
    assert tt.short("Captcha") == "Cap"
    assert tt.short("Cipher") == "Cip"


def test_a_three_letter_name_is_its_own_badge():
    assert tt.short("Hex") == "Hex"


def test_short_of_an_overflow_name_uses_the_stem():
    assert tt.short("Cipher-1a2b") == "Cip"
    assert tt.short("Velvet-1a2b") == "Vel"


def test_the_whole_cast_has_distinct_badges():
    """Two tabs wearing the same three letters is the one way this feature can
    be worse than no feature. Adding a name that collides breaks here first."""
    badges = [tt.short(name) for name in NAMES]
    assert len(set(badges)) == len(NAMES)


# ── the title ────────────────────────────────────────────────────────────────

def test_compose_puts_the_badge_first():
    assert tt.compose("Vex", "Rewrite the pipeline") == "Vex · Rewrite the pipeline"


def test_compose_falls_back_to_the_project():
    assert tt.compose("Mira", "", "dark-army") == "Mir · dark-army"


def test_compose_without_a_nickname_says_nothing():
    """No identity means no reason to write: the description alone is a worse
    version of the title Claude Code would have written itself."""
    assert tt.compose("", "Rewrite the pipeline") == ""


def test_compose_strips_control_characters():
    """A BEL in a session name would end the escape sequence early and print the
    remainder into the user's terminal."""
    out = tt.compose("Hex", "Fix \x07 the\nparser \x1b[31m")
    assert "\x07" not in out and "\n" not in out and "\x1b" not in out
    assert out.startswith("Hex · Fix")


def test_compose_is_bounded():
    assert len(tt.compose("Relay", "x" * 500)) == tt.MAX_TITLE


# ── the title of a row Dark Army launched ──────────────────────────────────────────

def test_title_for_is_compose_for_a_hand_started_row():
    row = {"nickname": "Vex", "name": "Rewrite the pipeline", "project": "p"}
    assert tt.title_for(row) == tt.compose("Vex", "Rewrite the pipeline", "p")


def test_an_unnamed_card_session_wears_the_cards_title():
    row = {"nickname": "Canon", "name": "New session",
           "card_title": "Mobile cards title", "origin_by": "card-start"}
    assert tt.title_for(row) == "Can · Mobile cards title"


def test_a_named_card_session_keeps_its_own_name():
    row = {"nickname": "Canon", "name": "Pick the area on the phone",
           "card_title": "Mobile cards title", "origin_by": "card-start"}
    assert tt.title_for(row) == "Can · Pick the area on the phone"


def test_a_planning_or_asking_tab_keeps_its_verb():
    row = {"nickname": "Canon", "name": "New session",
           "card_title": "Mobile cards title", "origin_by": "card-refine"}
    assert tt.title_for(row) == "Can · refine: Mobile cards title"
    row["origin_by"] = "card-consult"
    assert tt.title_for(row) == "Can · ask: Mobile cards title"


def test_a_verb_without_a_name_says_only_the_badge():
    row = {"nickname": "Canon", "name": "", "origin_by": "card-refine"}
    assert tt.title_for(row) == "Can"


def test_the_pass_names_a_launched_tab_by_its_card():
    term = FakeTerminal()
    w = _writer({101: "/dev/ttys001"}, term)
    row = _row(name="New session")
    row.update(card_title="Mobile cards title", origin_by="card-refine")
    w.apply({"running": [row]})
    assert term.writes == [("/dev/ttys001", "Vex · refine: Mobile cards title")]


# ── one pass over a snapshot ─────────────────────────────────────────────────

def test_a_live_session_gets_its_tab_named():
    term = FakeTerminal()
    w = _writer({101: "/dev/ttys001"}, term)
    w.apply({"running": [_row()]})
    assert term.writes == [("/dev/ttys001", "Vex · Rewrite the pipeline")]


def test_an_unchanged_snapshot_writes_nothing_twice():
    """The snapshot runs every few seconds all day; a pty write per session per
    tick for an identical string is the habit, not the exception, that matters."""
    term = FakeTerminal()
    w = _writer({101: "/dev/ttys001"}, term)
    snapshot = {"running": [_row()]}
    w.apply(snapshot)
    w.apply(snapshot)
    assert len(term.writes) == 1


def test_unchanged_codex_snapshot_does_not_restart_the_two_writer_loop():
    term = FakeTerminal()
    w = _writer({101: "/dev/ttys001"}, term)
    snapshot = {"running": [_row(pid=None, provider="codex")]}
    target = {"s1": "/dev/ttys001"}
    w.apply(snapshot, target)
    w.apply(snapshot, target)
    assert term.titles == ["Vex · Rewrite the pipeline"]


def test_codex_without_a_private_target_is_skipped_even_with_a_public_pid():
    term = FakeTerminal()
    w = _writer({101: "/dev/ttys001"}, term)
    w.apply({"running": [_row(provider="codex")]})
    w.apply({"running": [_row(provider="codex")]}, ["not", "a", "map"])
    assert term.writes == []


def test_codex_rows_on_one_tty_still_have_one_liveliest_owner():
    term = FakeTerminal()
    w = _writer({}, term)
    snapshot = {"running": [
        _row(sid="old", pid=None, provider="codex", nickname="Vex",
             idle_seconds=90.0),
        _row(sid="new", pid=None, provider="codex", nickname="Cipher",
             idle_seconds=2.0),
    ]}
    w.apply(snapshot, {"old": "/dev/ttys001", "new": "/dev/ttys001"})
    assert term.titles == ["Cip · Rewrite the pipeline"]


def test_refused_codex_direct_write_retries_without_polluting_pid_cache():
    term = FakeTerminal()
    term.refuse.add("/dev/ttys001")
    w = _writer({}, term)
    snapshot = {"running": [_row(pid=None, provider="codex")]}
    target = {"s1": "/dev/ttys001"}
    w.apply(snapshot, target)
    term.refuse.clear()
    w.apply(snapshot, target)
    assert term.titles == ["Vex · Rewrite the pipeline"]
    assert w._ttys == {}


def test_departed_codex_direct_target_is_forgotten():
    term = FakeTerminal()
    w = _writer({}, term)
    w.apply(
        {"running": [_row(pid=None, provider="codex")]},
        {"s1": "/dev/ttys001"},
    )
    w.apply({"running": []}, {"s1": "/dev/ttys001"})
    assert w._written == {} and w._ttys == {}


def test_a_changed_name_is_written_again():
    term = FakeTerminal()
    w = _writer({101: "/dev/ttys001"}, term)
    w.apply({"running": [_row()]})
    w.apply({"running": [_row(name="Ship the release")]})
    assert term.titles == ["Vex · Rewrite the pipeline", "Vex · Ship the release"]


def test_a_refused_write_is_retried_next_pass():
    term = FakeTerminal()
    term.refuse.add("/dev/ttys001")
    w = _writer({101: "/dev/ttys001"}, term)
    snapshot = {"running": [_row()]}
    w.apply(snapshot)
    term.refuse.clear()
    w.apply(snapshot)
    assert term.titles == ["Vex · Rewrite the pipeline"]


def test_finished_rows_are_left_alone():
    """The process is gone and its pid — hence its tty — may belong to somebody
    else's shell by now."""
    term = FakeTerminal()
    w = _writer({101: "/dev/ttys001"}, term)
    w.apply({"finished": [_row()]})
    assert term.writes == []


def test_a_grok_session_gets_the_same_badge():
    """VS Code never classifies a Grok tab as an agent CLI, so the process
    name (`grok-macos-aarch`) is what the user sees unless we write. Same
    compose as Claude — the primer is what makes the write render."""
    term = FakeTerminal()
    w = _writer({101: "/dev/ttys001"}, term)
    w.apply({"running": [_row(provider="grok", nickname="Quiet",
                              name="WOC asset analysis MVP plan")]})
    assert term.titles == ["Qui · WOC asset analysis MVP plan"]


def test_a_session_with_no_terminal_is_skipped():
    term = FakeTerminal()
    w = _writer({}, term)          # resolver returns "" — a background agent
    w.apply({"running": [_row()]})
    assert term.writes == []


def test_one_writer_per_tty_and_the_liveliest_wins():
    """`/clear` starts a new session id in the same tab while the old row is
    still in the snapshot. Both writing means the tab flickers between names."""
    term = FakeTerminal()
    w = _writer({101: "/dev/ttys001", 102: "/dev/ttys001"}, term)
    w.apply({"running": [
        _row(sid="old", pid=101, nickname="Vex", idle_seconds=90.0),
        _row(sid="new", pid=102, nickname="Cipher", idle_seconds=2.0),
    ]})
    assert term.titles == ["Cip · Rewrite the pipeline"]


def test_disabled_writes_nothing():
    term = FakeTerminal()
    w = _writer({101: "/dev/ttys001"}, term, enabled=False)
    w.apply({"running": [_row()]})
    assert term.writes == []


def test_departed_sessions_are_forgotten():
    """Both per-session maps are unbounded otherwise — the failure this codebase
    has already had to fix in several other session dicts."""
    term = FakeTerminal()
    w = _writer({101: "/dev/ttys001"}, term)
    w.apply({"running": [_row()]})
    w.apply({"running": []})
    assert w._written == {} and w._ttys == {}


def test_the_tty_is_resolved_once_per_session():
    calls = []

    def resolver(pid):
        calls.append(pid)
        return "/dev/ttys001"

    term = FakeTerminal()
    w = tt.TitleWriter(resolver=resolver)
    w._write = term.writer
    snapshot = {"running": [_row()]}
    w.apply(snapshot)
    w.apply(snapshot)
    assert calls == [101]


# ── the real tty write ───────────────────────────────────────────────────────

def test_write_emits_an_osc_sequence(tmp_path):
    """Against a FIFO rather than a pty: same non-blocking open, same os.write,
    and it asserts the exact bytes a terminal will parse — the primer first,
    the real title last, both in the one write."""
    path = tmp_path / "fake-tty"
    os.mkfifo(path)
    reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        w = tt.TitleWriter()
        assert w._write(str(path), "Vex · Rewrite") is True
        assert os.read(reader, 512) == (
            b"\033]0;Claude Code\007\033]0;Vex \xc2\xb7 Rewrite\007"
        )
    finally:
        os.close(reader)


def test_the_primer_satisfies_vscodes_classifier(tmp_path):
    """VS Code renders OSC titles only on terminals it has classified as an
    agent CLI, and the classifier is the title stream itself: a title matching
    /claude\\s*code/i — normally Claude Code's startup `✳ Claude Code`, the
    exact write our env flag suppresses. The primer stands in for it, and the
    real title must still be the last one in the payload, because every
    terminal keeps only the last title of a write."""
    import re
    path = tmp_path / "fake-tty"
    os.mkfifo(path)
    reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        w = tt.TitleWriter()
        assert w._write(str(path), "Vex · Rewrite") is True
        titles = re.findall(r"\x1b\]0;(.*?)\x07",
                            os.read(reader, 512).decode())
        assert re.search(r"claude\s*code", titles[0], re.IGNORECASE)
        assert titles[-1] == "Vex · Rewrite"
    finally:
        os.close(reader)


def test_write_on_a_dead_terminal_is_a_refusal(tmp_path):
    w = tt.TitleWriter()
    assert w._write(str(tmp_path / "gone"), "Vex") is False


# ── tty lookup ───────────────────────────────────────────────────────────────

def test_tty_path_of_this_process_or_none():
    """Under pytest there may be no controlling terminal at all, which is itself
    the contract: "" rather than a guess."""
    out = tt.tty_path_for(os.getpid())
    assert out == "" or out.startswith("/dev/")


def test_a_review_and_a_merge_fix_tab_keep_their_verbs():
    for by, prefix in (("card-review", "review: "), ("card-merge-fix", "fix: ")):
        assert tt.ORIGIN_PREFIX[by] == prefix
        row = {"nickname": NAMES[0], "name": "Add the thing", "origin_by": by}
        assert tt.title_for(row).endswith(f"{prefix}Add the thing")
