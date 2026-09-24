# host/tests/test_vtgrid.py
"""The escape-sequence contract of `vtgrid.Screen`, in scope and out.

Every in-scope sequence is fed both whole and **byte-wise** — a CSI cut in
half by a read boundary is the classic emulator bug — and the parametrised
cases below run each fixture through both feeders. Out-of-scope sequences
(DCS strings, device queries, an unknown CSI final) must be *consumed*, never
drawn as text.
"""

from __future__ import annotations

import pytest

from dark_army_daemon import vtgrid
from dark_army_daemon.vtgrid import (A_BOLD, A_INVERSE, A_UNDERLINE,
                                         DEFAULT, TRUECOLOR, Screen,
                                         runs_of)


def whole(screen: Screen, data: bytes) -> None:
    screen.feed(data)


def bytewise(screen: Screen, data: bytes) -> None:
    for b in data:
        screen.feed(bytes([b]))


FEEDERS = [whole, bytewise]


@pytest.fixture(params=FEEDERS, ids=["whole", "bytewise"])
def feed(request):
    return request.param


def cell(screen, x, y):
    return screen.lines[y][x]


# --- C0 controls --------------------------------------------------------------


def test_plain_text_lands_left_to_right(feed):
    s = Screen(10, 3)
    feed(s, b"hello")
    assert s.text() == ["hello", "", ""]
    assert s.cursor == (5, 0)


def test_cr_and_lf_move_the_cursor(feed):
    s = Screen(10, 3)
    feed(s, b"ab\r\ncd\rX")
    assert s.text() == ["ab", "Xd", ""]


def test_backspace_and_tab(feed):
    s = Screen(20, 2)
    feed(s, b"abc\bX\tY")
    # BS moved onto `c`, X overwrote it; HT jumped to column 8.
    assert s.text()[0] == "abX     Y"


def test_bell_is_counted_not_drawn(feed):
    s = Screen(10, 2)
    feed(s, b"a\x07b")
    assert s.text()[0] == "ab"
    assert s.bells == 1


def test_snapshot_restore_round_trips_the_grid_and_history(feed):
    s = Screen(5, 2)
    feed(s, b"one\r\ntwo\r\nthree")
    snap = s.snapshot()
    assert snap["history"]
    other = Screen(8, 8)
    other.restore(snap)
    assert other.cols == 5 and other.rows == 2
    assert other.text() == s.text()
    assert other.history_runs() == s.history_runs()
    assert other.cursor == s.cursor


def test_lf_at_the_bottom_scrolls_into_scrollback(feed):
    s = Screen(5, 2)
    feed(s, b"one\r\ntwo\r\nthree")
    assert s.text() == ["two", "three"]
    assert ["".join(c[0] for c in row).rstrip() for row in s.scrollback] == ["one"]
    assert s.history_runs()[0][0][0] == "one"


# --- cursor movement ----------------------------------------------------------


def test_cup_cha_vpa_and_the_four_arrows(feed):
    s = Screen(10, 5)
    feed(s, b"\x1b[3;4HA")          # row 3, col 4
    assert cell(s, 3, 2)[0] == "A"
    feed(s, b"\x1b[2GB")            # CHA col 2
    assert cell(s, 1, 2)[0] == "B"
    feed(s, b"\x1b[5dC")            # VPA row 5
    assert cell(s, 2, 4)[0] == "C"
    feed(s, b"\x1b[2A\x1b[3D\x1b[1CD")  # up 2, left 3, right 1
    assert cell(s, 1, 2)[0] == "D"
    feed(s, b"\x1b[1BE")            # down 1
    assert cell(s, 2, 3)[0] == "E"


def test_movement_clamps_at_the_edges(feed):
    s = Screen(5, 3)
    feed(s, b"\x1b[99;99HX")
    assert cell(s, 4, 2)[0] == "X"
    feed(s, b"\x1b[99A\x1b[99DY")
    assert cell(s, 0, 0)[0] == "Y"


def test_cnl_and_cpl_go_to_column_zero(feed):
    s = Screen(10, 4)
    feed(s, b"abc\x1b[1EX\x1b[1FY")
    assert s.text()[:2] == ["Ybc", "X"]


def test_save_and_restore_cursor_both_forms(feed):
    s = Screen(10, 3)
    feed(s, b"ab\x1b7\r\n\r\nzz\x1b8C")
    assert s.text()[0] == "abC"
    feed(s, b"\x1b[s\r\n\x1b[uD")
    assert s.text()[0] == "abCD"


def test_autowrap_wraps_at_the_last_column_and_can_be_switched_off(feed):
    s = Screen(4, 3)
    feed(s, b"abcdef")
    assert s.text()[:2] == ["abcd", "ef"]
    s2 = Screen(4, 3)
    feed(s2, b"\x1b[?7labcdef")
    assert s2.text() == ["abcf", "", ""]


# --- erasing ------------------------------------------------------------------


@pytest.mark.parametrize("param,expected", [
    (b"0", ["abc", "d", ""]),     # from the cursor to the end of the screen
    (b"1", ["", "", "ghi"]),      # from the start to the cursor, inclusive
    (b"2", ["", "", ""]),         # everything
])
def test_erase_display_every_parameter(feed, param, expected):
    s = Screen(3, 3)
    feed(s, b"abc\r\ndef\r\nghi\x1b[2;3H")
    # cursor is on row 2 (1-based), col 3 — the `f`.
    if param == b"0":
        feed(s, b"\x1b[2;2H")
    feed(s, b"\x1b[" + param + b"J")
    assert s.text() == expected


@pytest.mark.parametrize("param,expected", [
    (b"0", "ab"), (b"1", "   de"), (b"2", ""), (b"", "ab"),
])
def test_erase_line_every_parameter(feed, param, expected):
    s = Screen(5, 1)
    feed(s, b"abcde\x1b[3G\x1b[" + param + b"K")
    assert s.text()[0] == expected


def test_erase_display_3_clears_scrollback_only(feed):
    s = Screen(3, 1)
    feed(s, b"a\r\nb")
    assert len(s.scrollback) == 1
    feed(s, b"\x1b[3J")
    assert len(s.scrollback) == 0
    assert s.text() == ["b"]


# --- insert / delete ----------------------------------------------------------


def test_insert_and_delete_lines(feed):
    s = Screen(3, 4)
    feed(s, b"a\r\nb\r\nc\r\nd\x1b[2;1H\x1b[1L")
    assert s.text() == ["a", "", "b", "c"]
    feed(s, b"\x1b[2M")
    assert s.text() == ["a", "c", "", ""]


def test_insert_delete_and_erase_chars(feed):
    s = Screen(6, 1)
    feed(s, b"abcdef\x1b[2G\x1b[2@")
    assert s.text()[0] == "a  bcd"
    feed(s, b"\x1b[2P")
    assert s.text()[0] == "abcd"
    feed(s, b"\x1b[1X")
    assert s.text()[0] == "a cd"


# --- scroll region ------------------------------------------------------------


def test_decstbm_scrolls_only_inside_the_region(feed):
    s = Screen(3, 4)
    feed(s, b"top\r\n1\r\n2\r\nbot")
    feed(s, b"\x1b[2;3r")            # rows 2..3 are the region; cursor homes
    feed(s, b"\x1b[3;1H\nX")         # LF at the region's bottom scrolls it
    assert s.text() == ["top", "2", "X", "bot"]


def test_reverse_index_at_the_region_top_scrolls_down(feed):
    s = Screen(3, 3)
    feed(s, b"a\r\nb\r\nc\x1b[1;1H\x1bMZ")
    assert s.text() == ["Z", "a", "b"]


def test_su_and_sd(feed):
    s = Screen(2, 3)
    feed(s, b"a\r\nb\r\nc\x1b[1S")
    assert s.text() == ["b", "c", ""]
    feed(s, b"\x1b[1T")
    assert s.text() == ["", "b", "c"]


# --- SGR ----------------------------------------------------------------------


def test_sgr_attributes_and_their_resets(feed):
    s = Screen(10, 1)
    feed(s, b"\x1b[1;4;7mA\x1b[22;24;27mB\x1b[0mC")
    assert cell(s, 0, 0)[3] == A_BOLD | A_UNDERLINE | A_INVERSE
    assert cell(s, 1, 0)[3] == 0
    assert cell(s, 2, 0)[3] == 0


@pytest.mark.parametrize("seq,fg,bg", [
    (b"\x1b[31;42m", 1, 2),
    (b"\x1b[97;100m", 15, 8),
    (b"\x1b[38;5;196;48;5;21m", 196, 21),
    (b"\x1b[38:5:200m", 200, DEFAULT),
    (b"\x1b[38;2;10;20;30m", TRUECOLOR | (10 << 16) | (20 << 8) | 30, DEFAULT),
    (b"\x1b[48;2;1;2;3m", DEFAULT, TRUECOLOR | (1 << 16) | (2 << 8) | 3),
    (b"\x1b[38:2::4:5:6m", TRUECOLOR | (4 << 16) | (5 << 8) | 6, DEFAULT),
])
def test_sgr_colours_in_every_form(feed, seq, fg, bg):
    s = Screen(4, 1)
    feed(s, seq + b"X")
    assert cell(s, 0, 0)[1] == fg
    assert cell(s, 0, 0)[2] == bg


def test_sgr_39_and_49_restore_the_defaults(feed):
    s = Screen(4, 1)
    feed(s, b"\x1b[31;42m\x1b[39mA\x1b[49mB")
    assert cell(s, 0, 0)[1:3] == (DEFAULT, 2)
    assert cell(s, 1, 0)[1:3] == (DEFAULT, DEFAULT)


def test_sgr_state_carries_across_rows(feed):
    s = Screen(4, 2)
    feed(s, b"\x1b[31mab\r\ncd")
    assert cell(s, 0, 1)[1] == 1
    assert cell(s, 1, 1)[1] == 1


# --- DEC private modes --------------------------------------------------------


def test_cursor_visibility_and_bracketed_paste(feed):
    s = Screen(4, 1)
    feed(s, b"\x1b[?25l\x1b[?2004h")
    assert s.cursor_visible is False
    assert s.bracketed_paste is True
    feed(s, b"\x1b[?25h\x1b[?2004l")
    assert s.cursor_visible is True
    assert s.bracketed_paste is False


def test_paint_re_emits_bracketed_paste_so_a_multi_line_paste_survives(feed):
    """The panel's pane is a mirror of this emulator: every attach and every
    reattach starts from `paint()`. If the mode were not replayed, a paste of
    several lines would arrive at the agent as several *sends* — one per
    newline — the first time the pane reconnected."""
    s = Screen(4, 1)
    feed(s, b"\x1b[?2004h")
    assert s.bracketed_paste is True
    assert b"\x1b[?2004h" in s.paint()
    feed(s, b"\x1b[?2004l")
    assert b"\x1b[?2004h" not in s.paint()


def test_alt_screen_enter_and_leave_restore_the_primary_grid(feed):
    s = Screen(5, 2)
    feed(s, b"main\x1b[?1049h")
    assert s.alt_screen is True
    assert s.text() == ["", ""]
    feed(s, b"alt")
    assert s.text() == ["alt", ""]
    feed(s, b"\x1b[?1049l")
    assert s.alt_screen is False
    assert s.text() == ["main", ""]
    assert s.cursor == (4, 0)


def test_alt_screen_scrolling_never_reaches_scrollback(feed):
    s = Screen(3, 1)
    feed(s, b"\x1b[?1049ha\r\nb\r\nc")
    assert len(s.scrollback) == 0


# --- OSC and out-of-scope strings --------------------------------------------


@pytest.mark.parametrize("seq", [b"\x1b]0;my title\x07", b"\x1b]2;my title\x1b\\"])
def test_osc_title_is_published_never_drawn(feed, seq):
    s = Screen(20, 1)
    feed(s, seq + b"X")
    assert s.title == "my title"
    assert s.text()[0] == "X"


def test_an_unknown_csi_final_is_consumed(feed):
    s = Screen(10, 1)
    feed(s, b"a\x1b[1;2zb\x1b[?1000hc")
    assert s.text()[0] == "abc"


def test_dcs_and_apc_strings_are_consumed(feed):
    s = Screen(10, 1)
    feed(s, b"a\x1bPq#0;2;0;0;0#0!14~-\x1b\\b\x1b_G\x07c")
    assert s.text()[0] == "abc"


def test_a_control_inside_a_csi_is_executed(feed):
    s = Screen(10, 2)
    feed(s, b"ab\x1b[\r\n2Cx")
    # CR LF ran mid-sequence (xterm's rule), then the CSI finished.
    assert s.text()[1] == "  x"


# --- width ------------------------------------------------------------------


def test_a_wide_character_takes_two_cells(feed):
    s = Screen(6, 1)
    feed(s, "日本x".encode())
    assert cell(s, 0, 0)[0] == "日"
    assert cell(s, 1, 0)[0] == ""
    assert cell(s, 4, 0)[0] == "x"
    assert runs_of(s.lines[0])[0][0] == "日本x"


def test_a_combining_mark_joins_the_previous_cell(feed):
    s = Screen(4, 1)
    feed(s, "éx".encode())
    assert cell(s, 0, 0)[0] == "é"
    assert cell(s, 1, 0)[0] == "x"


def test_a_utf8_sequence_split_across_chunks_decodes_whole():
    s = Screen(4, 1)
    data = "é".encode()
    s.feed(data[:1])
    s.feed(data[1:])
    assert s.text()[0] == "é"


# --- revisions, since and overflow --------------------------------------------


def test_since_returns_only_the_dirty_rows():
    s = Screen(5, 3)
    s.feed(b"a")
    rev1, rows, overflowed = s.since(0)
    assert rev1 == 1 and [y for y, _ in rows] == [0] and overflowed is False
    s.feed(b"\x1b[3;1Hc")
    rev2, rows, overflowed = s.since(rev1)
    assert rev2 == 2 and [y for y, _ in rows] == [2] and overflowed is False
    rev3, rows, _ = s.since(rev2)
    assert rev3 == rev2 and rows == []


def test_rows_are_ordered_by_the_revision_that_dirtied_them():
    s = Screen(5, 3)
    s.feed(b"\x1b[3;1Hc")
    s.feed(b"\x1b[1;1Ha")
    _rev, rows, _ = s.since(0)
    assert [y for y, _ in rows] == [2, 0]
    assert s.row_revision(2) < s.row_revision(0)


def test_a_resize_overflows_and_sends_a_full_frame():
    s = Screen(5, 2)
    s.feed(b"ab")
    rev, _, _ = s.since(0)
    s.resize(8, 3)
    rev2, rows, overflowed = s.since(rev)
    assert overflowed is True
    assert rev2 > rev
    assert [y for y, _ in rows] == [0, 1, 2]
    assert s.text() == ["ab", "", ""]
    assert (s.cols, s.rows) == (8, 3)


def test_a_revision_from_another_life_overflows():
    s = Screen(5, 2)
    s.feed(b"ab")
    _rev, rows, overflowed = s.since(10 ** 9)
    assert overflowed is True
    assert len(rows) == 2


def test_alt_screen_swap_overflows_the_caller():
    s = Screen(5, 2)
    s.feed(b"ab")
    rev, _, _ = s.since(0)
    s.feed(b"\x1b[?1049h")
    _rev2, rows, overflowed = s.since(rev)
    assert overflowed is True
    assert len(rows) == 2


def test_runs_merge_style_and_drop_trailing_blanks():
    s = Screen(8, 1)
    s.feed(b"\x1b[1mab\x1b[0mc")
    assert runs_of(s.lines[0]) == [["ab", DEFAULT, DEFAULT, A_BOLD],
                                   ["c", DEFAULT, DEFAULT, 0]]
    assert runs_of(Screen(3, 1).lines[0]) == []


def test_the_bounds_clamp_the_constructor_and_resize():
    s = Screen(1, 0)
    assert (s.cols, s.rows) == (vtgrid.MIN_COLS, vtgrid.MIN_ROWS)
    s.resize(10 ** 6, 10 ** 6)
    assert (s.cols, s.rows) == (vtgrid.MAX_COLS, vtgrid.MAX_ROWS)


def test_the_module_does_no_io_and_no_asyncio():
    import inspect
    src = inspect.getsource(vtgrid)
    for word in ("import asyncio", "import os", "open(", "import socket"):
        assert word not in src
