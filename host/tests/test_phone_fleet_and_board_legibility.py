"""Pins for the phone's two-line fleet row and richer board cards.

`ios/` has no test target by explicit decision, so this is a Python lint over
the Swift source — `test_phone_theme_drift.py`'s pattern. It pins the layout
arithmetic, the tolerant-decode rule, wire-key parity with the daemon (the
phone reads nothing the Mac does not publish), the shared manual-check wording,
and that writes go through `/api/action` without opening SSE. A moved file
or an emptied pin must fail loudly.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
DAEMON = ROOT / "host" / "dark_army_daemon"
PANEL_CARD = ROOT / "panel" / "Sources" / "BobPanel" / "BoardCardView.swift"

PROCESS = PHONE / "ProcessTable.swift"
MODELS = PHONE / "Models.swift"
CARD_DETAIL = PHONE / "CardDetailView.swift"
BOARD = PHONE / "BoardView.swift"
ANSWER = PHONE / "AnswerBox.swift"
FLEET = PHONE / "FleetView.swift"
NEEDS_YOU = PHONE / "NeedsYouView.swift"
PANEL_PROCESS = ROOT / "panel" / "Sources" / "BobPanel" / "ProcessTable.swift"
PANEL_VIEW = ROOT / "panel" / "Sources" / "BobPanel" / "PanelView.swift"

WIRE_KEYS = (
    "tool",
    "link_state",
    "dispatch_error",
    "plan_path",
    "queue_state",
    "queue_reason",
    "manual_check_due",
    "needs_you",
    "manual_steps",
    "prompt",
    "prompt_truncated",
    "summary_truncated",
    "close_note",
    "closed_by_name",
    "counts",
    "branch",
    "model",
    "can_type",
    "can_stop",
    "can_close",
    "channel",
    "reply_options",
    "options",
    "header",
    "dispatch_enabled",
    "tools",
    "projects",
    "session_id",
    "refine_state",
    "create_token",
    "notifications",
    "permissions",
    "trend",
    "subagent_rows",
    "card_title",
)

DAEMON_PIN_FILES = (
    DAEMON / "daemon.py",
    DAEMON / "daemon_board.py",   # the board verbs moved here whole (pure move)
    DAEMON / "board.py",
    DAEMON / "session_stats.py",
)

BADGE = "MANUAL CHECK NEEDED"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def test_the_sources_are_there():
    assert PROCESS.is_file()
    assert MODELS.is_file()
    assert CARD_DETAIL.is_file()
    assert BOARD.is_file()
    assert PANEL_CARD.is_file()
    for path in DAEMON_PIN_FILES:
        assert path.is_file(), f"missing daemon pin file: {path}"
    assert list(PHONE.glob("*.swift")), "no Swift sources under ios/BobPhone"


def test_the_name_column_is_no_longer_fixed_at_76pt():
    text = _read(PROCESS)
    assert "width: 76" not in text
    # Anti-vacuous: the file still describes a row with a name.
    assert "NAME" in text
    assert "PhoneProcessRow" in text


def test_the_work_description_wraps_on_line_two():
    """The three-line ceiling came off deliberately — the work description is
    the whole point of line 2 and the dots were hiding it. The rest of the
    sweep, and which caps are still allowed to exist, is
    `test_phone_text_in_full.py`."""
    text = _read(PROCESS)
    assert "lineLimit(3)" not in text, (
        "the command line's ceiling is gone on purpose; see "
        "test_phone_text_in_full.py")
    assert '"CMD"' not in text
    # Anti-vacuous: line 2 still draws the command, the file is not empty of
    # caps (AGE keeps its fixed-width one), and the line can actually wrap.
    assert "command" in text
    assert "lineLimit(" in text
    assert "fixedSize(horizontal: false, vertical: true)" in text


def test_models_and_card_detail_decode_tolerantly():
    banned = "try c.decode("
    for path in (MODELS, CARD_DETAIL):
        text = _read(path)
        assert banned not in text, f"{path.name} uses synthesized-style decode"
        # Anti-vacuous: Models still decodes through the helpers.
        if path == MODELS:
            assert "c.value(" in text
            assert "c.maybe(" in text
            assert "struct WorkRecordHead" in text


def test_the_phone_reads_only_keys_the_daemon_publishes():
    assert WIRE_KEYS, "wire-key list must not be empty"
    daemon = "\n".join(_read(path) for path in DAEMON_PIN_FILES)
    phone = _read(MODELS) + _read(CARD_DETAIL) + _read(BOARD) + _read(ANSWER)
    for key in WIRE_KEYS:
        assert key in daemon, (
            f"wire key {key!r} is not in daemon.py/daemon_board.py/board.py/"
            "session_stats.py "
            "— the phone must not invent fields the Mac does not publish")
        assert key in phone, (
            f"wire key {key!r} is missing from the phone source")


def test_the_command_line_prefers_the_card_title():
    """The daemon publishes `card_title` only beside its own placeholder name,
    which is non-empty — so a rung below `agent.name` would never fire."""
    text = _read(PROCESS)
    match = re.search(r"private var command: String \{(.*?)\n    \}", text, re.S)
    assert match, "no `command` ladder in the phone's process row"
    body = match.group(1)
    assert "agent.cardTitle" in body
    assert body.index("agent.cardTitle") < body.index("agent.name")


def test_the_manual_badge_is_the_panel_s_own_words():
    panel = _read(PANEL_CARD)
    assert f'Text("{BADGE}")' in panel, (
        f"panel BoardCardView.swift no longer carries {BADGE!r}")
    phone = _read(BOARD) + _read(CARD_DETAIL)
    assert f'Text("{BADGE}")' in phone, (
        f"phone board/detail no longer carry {BADGE!r}")
    # Byte-identical, not a paraphrase.
    assert BADGE in phone
    assert BADGE in panel


def test_the_phone_posts_actions_and_never_opens_sse():
    swift_files = list(PHONE.glob("*.swift"))
    assert swift_files, "no Swift sources to sweep"
    action_files = []
    post_files = []
    for path in swift_files:
        text = path.read_text()
        if "/api/action" in text:
            action_files.append(path.name)
        if 'httpMethod = "POST"' in text:
            post_files.append(path.name)
        assert "/api/events" not in text, path.name
        assert "text/event-stream" not in text, path.name
    assert "Actions.swift" in action_files, (
        "/api/action must live in the actions helper")
    assert "Pairing.swift" in post_files
    # The client's six home legs ride `HomeChannel`, so the POST that used
    # to live in Client.swift is HomeTransport.swift's now.
    assert "HomeTransport.swift" in post_files
    pairing = _read(PHONE / "Pairing.swift")
    assert "/api/pair" in pairing
    assert "/api/action" not in pairing


@pytest.mark.parametrize(
    "path",
    [PROCESS, MODELS, CARD_DETAIL, BOARD, ANSWER, FLEET, PANEL_CARD,
     *DAEMON_PIN_FILES],
)
def test_pin_files_exist(path):
    """A missing file is a failure, never a skip."""
    assert path.is_file(), f"missing file: {path}"


# --- Fleet chips carry a live count -----------------------------------------

LIVE_PREDICATE = (
    "$0.category == .waiting || $0.category == .running"
    " || $0.category == .sleeping"
)


def _body(text: str, decl: str) -> str:
    """The text from `decl` to the next `private` declaration at the same
    indent, so a pin on one computed property cannot be satisfied by another."""
    start = text.index(decl)
    end = text.find("\n    private ", start + len(decl))
    return text[start:end if end != -1 else len(text)]


def _fleet_is_still_the_fleet(text: str) -> None:
    # Anti-vacuous: the file still draws the strip and the rows.
    assert "PhoneProcessRow" in text
    assert "projectStrip" in text


def test_fleet_chips_draw_a_live_count():
    text = _read(FLEET)
    _fleet_is_still_the_fleet(text)
    signature = text[text.index("private func chip("):]
    signature = signature[:signature.index("{")]
    assert "count: Int" in signature
    assert text.count('Text("\\(count)")') == 1
    assert text.count(".monospacedDigit()") >= 1
    count_text = text[text.index('Text("\\(count)")'):]
    count_text = count_text[:count_text.index("if dot")]
    assert "Theme.mono(10)" in count_text


def test_fleet_chip_counts_come_from_the_list_s_own_live_rows():
    text = _read(FLEET)
    _fleet_is_still_the_fleet(text)
    counts = _body(text, "private var liveCounts")
    assert "allLive" in counts
    assert "liveRows" not in counts, "counts must not read the filtered list"
    assert "snapshot.agents" not in counts, "counts must not walk the buckets"
    live = _body(text, "private var liveRows")
    assert "allLive" in live
    assert text.count(LIVE_PREDICATE) == 1, (
        "the live predicate must appear exactly once — a second walk is drift")


def test_the_all_chip_shows_the_total():
    text = _read(FLEET)
    _fleet_is_still_the_fleet(text)
    # The strip is shared with the Board tab (`PhoneProjectStrip`): Fleet
    # hands it the live total, and the strip's `ALL` chip draws that total.
    assert text.count("total: allLive.count") == 1
    assert "total: allLive.count" in _body(text, "private var projectStrip")
    line = next(l for l in text.splitlines() if "count: total)" in l)
    assert 'title: "ALL"' in line


def test_fleet_chip_order_and_size_are_unchanged():
    text = _read(FLEET)
    _fleet_is_still_the_fleet(text)
    # The order moved into `FleetProjects.names`, which `projects` delegates
    # to — the chips are still sorted, and the pin follows the sort rather
    # than being dropped.
    assert "FleetProjects.names(" in _body(text, "private var projects")
    names = text[text.index("static func names("):]
    names = names[:names.index("\n    static func ")]
    assert ".sorted()" in names
    assert text.count("minHeight: 32") == 1
    assert text.count("padding(.vertical, 6)") == 2
    assert "Circle().fill(Color.red)" in text


def test_fleet_view_is_not_byte_pinned_to_the_panel():
    text = _read(FLEET)
    _fleet_is_still_the_fleet(text)
    drift = _read(Path(__file__).with_name("test_phone_theme_drift.py"))
    assert "FleetView" not in drift, (
        "the panel has no FleetView; a byte pairing would fail on line one")


# --- AGE column, shared FleetAge.text -----------------------------------------

_TEXT_NEEDLE = "static func text(startedAt"
_SPOKEN_NEEDLE = "static func spoken(startedAt"
_TICK = "TimelineView(.periodic(from: .now, by: 1))"


def _brace_chunk(text, start):
    brace = text.find("{", start)
    assert brace >= 0, f"no opening brace after {start}"
    depth = 0
    for i, ch in enumerate(text[brace:], brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError("unclosed brace")


def _func(text, needle):
    start = text.find(needle)
    assert start >= 0, f"missing {needle!r}"
    return _brace_chunk(text, start)


def _struct(text, name):
    start = text.find(f"struct {name}")
    assert start >= 0, f"no struct {name}"
    return _brace_chunk(text, start)


def test_fleet_age_text_and_spoken_match_the_panel():
    phone = _read(PROCESS)
    panel = _read(PANEL_PROCESS)
    phone_text = _func(phone, _TEXT_NEEDLE)
    panel_text = _func(panel, _TEXT_NEEDLE)
    assert phone_text == panel_text
    assert '"—"' in phone_text
    assert "< 60" in phone_text
    assert "< 3600" in phone_text
    phone_spoken = _func(phone, _SPOKEN_NEEDLE)
    panel_spoken = _func(panel, _SPOKEN_NEEDLE)
    assert phone_spoken == panel_spoken
    assert "< 60" in phone_spoken
    assert "< 3600" in phone_spoken
    assert 's == 1 ? "1 second"' in phone_spoken
    assert "spokenAge" not in panel


def test_phone_age_column_uses_one_width():
    text = _read(PROCESS)
    row = _struct(text, "PhoneProcessRow")
    assert "FleetAge.width" in row
    assert "FleetAge.width" in _struct(text, "PhoneProcessHeader")
    assert "FleetAge.spoken" in row
    assert "accessibilityHidden" in row


def test_phone_age_reads_started_at_not_duration_seconds():
    text = _read(PROCESS)
    assert "startedAt" in text
    assert "durationSeconds" not in text


def test_phone_age_ticks_once_a_second():
    text = _read(PROCESS)
    assert text.count(_TICK) == 1
    assert text.count('Text("AGE")') == 1


def test_sort_order_is_untouched():
    fleet = _read(FLEET)
    sorted_fn = _func(fleet, "private func sorted(")
    assert "l > r" in sorted_fn
    assert "case (nil, _?): return false" in sorted_fn
    assert "case (_?, nil): return true" in sorted_fn
    panel = _read(PANEL_VIEW)
    by_start = _func(panel, "private func byStart(")
    assert "ka < kb" in by_start


def test_needs_you_draws_compact_rows_that_clip_nothing():
    """The tab draws its own short row (20 Sep 2026) — face, word, title,
    age, detail in full — not the fleet's five-column row, and caps
    nothing: the phone does not clip prose."""
    text = _read(NEEDS_YOU)
    assert "PhoneProcessRow" not in text
    assert "struct InboxRow" in text
    assert "FleetAge.text(startedAt: item.since" in text
    assert ".lineLimit(" not in text
    assert text.count("fixedSize(horizontal: false, vertical: true)") >= 4
