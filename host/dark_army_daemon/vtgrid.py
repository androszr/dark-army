"""A small terminal emulator: raw bytes in, a grid of cells out.

Dark Army draws the terminals it hosts (`ptyhost.py`) in its own panel and on the
phone, and neither of those is an emulator — the panel has no package
dependencies by rule, and two emulators would be two sets of bugs. So the
stream is parsed **once, here, on the daemon**, and every client draws the
grid this produces. Pure: no I/O, no asyncio, no clock. `feed()` takes bytes
in whatever chunks the pty reader happens to deliver — a CSI cut in half by
a read boundary is the classic emulator bug, so the parser keeps its state
across calls and the tests feed every sequence byte-wise as well as whole.

**In scope**, and exactly this: CR, LF (VT, FF), BS, HT, BEL; CSI CUU / CUD /
CUF / CUB / CNL / CPL / CUP / CHA / VPA; ED and EL with every parameter; IL /
DL / ICH / DCH / ECH; SU / SD; DECSTBM; SGR 0 1 2 3 4 7 22 23 24 27 39 49,
30–37, 40–47, 90–97, 100–107, `38;5;n`, `48;5;n`, `38;2;r;g;b`,
`48;2;r;g;b`; DEC private modes ?25 (cursor), ?1049 (alternate screen, with
?47 / ?1047 as aliases), ?2004 (bracketed paste), ?7 (autowrap); ESC 7 / ESC
8 and CSI s / u (save and restore cursor); ESC D / ESC M / ESC E; OSC 0 / 2
as the title. Everything else — sixel and kitty graphics, mouse reporting,
DCS strings, device queries — is consumed silently rather than drawn, and
Dark Army advertises `TERM=xterm-256color` so the CLIs negotiate down rather than
assume. There is no reflow on resize: the grid is re-laid to the new size
and history is not rewrapped. Width is a simple wide/narrow table
(`east_asian_width`), not a full `wcwidth`.

**The wire shape** is rows of *runs* rather than cells: `[text, fg, bg,
attrs]`, consecutive cells with one style merged, trailing default blanks
dropped. `since(revision)` returns only rows dirtied after the caller's
revision — `card_sync`'s discipline — ordered by the revision that dirtied
them, so a caller bounded by bytes can take a prefix and quote the last
row's revision back to continue.
"""
from __future__ import annotations

import codecs
import unicodedata
from collections import deque
from typing import Optional

#: Default colours on the wire. Palette entries are 0..255; a truecolour
#: value is `TRUECOLOR | (r << 16) | (g << 8) | b`.
DEFAULT = -1
TRUECOLOR = 0x1000000

A_BOLD = 1
A_DIM = 2
A_UNDERLINE = 4
A_INVERSE = 8
A_ITALIC = 16

#: Lines kept above the primary screen. Memory only, never a file.
SCROLLBACK_LINES = 500

#: Bytes `defer()` holds back before parsing them anyway. The grid is read
#: far less often than it is written — the phone polls it once a second,
#: the panel's attach paints from it once — so parsing rides the read, in
#: one batch, rather than the loop's pty reader byte by byte. The cap
#: bounds both the memory and the size of any one catch-up.
LAG_SYNC_BYTES = 64 * 1024

#: DEC private modes `paint()` states explicitly, so the generic replay
#: skips them: cursor visibility, autowrap, bracketed paste and the
#: alternate screen family.
_PAINTED_MODES = frozenset({25, 7, 2004, 1049, 1047, 47})

MIN_COLS, MAX_COLS = 2, 400
MIN_ROWS, MAX_ROWS = 1, 200

BLANK = (" ", DEFAULT, DEFAULT, 0)

_GROUND, _ESC, _CSI, _OSC, _STR, _CHARSET = range(6)


def char_width(ch: str) -> int:
    """0 for a combining mark, 2 for an East Asian wide/full-width
    character, 1 otherwise. Deliberately a table, not `wcwidth`."""
    if not ch:
        return 0
    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def runs_of(row: list) -> list:
    """A row's cells merged into `[text, fg, bg, attrs]` runs, trailing
    default blanks dropped."""
    out: list = []
    for ch, fg, bg, attr in row:
        if out and out[-1][1] == fg and out[-1][2] == bg and out[-1][3] == attr:
            out[-1][0] += ch
        else:
            out.append([ch, fg, bg, attr])
    # Trailing default-style blanks are padding, not content: strip them
    # off the last run and drop it if nothing is left.
    if out and out[-1][1] == DEFAULT and out[-1][2] == DEFAULT and out[-1][3] == 0:
        out[-1][0] = out[-1][0].rstrip(" ")
        if out[-1][0] == "":
            out.pop()
    return out


class Screen:
    """A `cols` × `rows` grid of `(char, fg, bg, attrs)` cells plus the parser
    state that fills it."""

    def __init__(self, cols: int = 80, rows: int = 24,
                 scrollback: int = SCROLLBACK_LINES):
        self.cols = max(MIN_COLS, min(MAX_COLS, int(cols)))
        self.rows = max(MIN_ROWS, min(MAX_ROWS, int(rows)))
        self.lines: list = [self._blank_row() for _ in range(self.rows)]
        self._scrollback: deque = deque(maxlen=max(0, int(scrollback)))
        self.revision = 0
        self.title = ""
        self.bells = 0
        self.cursor_visible = True
        self.alt_screen = False
        self.bracketed_paste = False
        self.autowrap = True
        self._x = 0
        self._y = 0
        self._pending_wrap = False
        self._top = 0
        self._bottom = self.rows - 1
        self._fg = DEFAULT
        self._bg = DEFAULT
        self._attr = 0
        self._saved_cursor = (0, 0, DEFAULT, DEFAULT, 0)
        self._primary: Optional[tuple] = None
        self._dirty: dict = {}
        self._floor = 0
        self._state = _GROUND
        self._buf = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        # Bytes taken by `defer()` and not yet parsed; `_sync()` drains it.
        self._lag = bytearray()
        # Every DEC private mode currently on (`?Nh`), so an attach can
        # replay the application's own settings — cursor keys, mouse
        # reporting, focus events — into a fresh viewer.
        self._modes: set = set()

    # ── public ───────────────────────────────────────────────────────────────

    @property
    def scrollback(self) -> deque:
        """Lines above the live screen, oldest first. Synced on read."""
        self._sync()
        return self._scrollback

    @property
    def cursor(self) -> tuple:
        self._sync()
        return (min(self._x, self.cols - 1), self._y)

    def feed(self, data) -> None:
        """Parse `data` (bytes or str) into the grid. Chunk boundaries are
        immaterial: parser state survives between calls."""
        self._sync()
        self._feed_now(data)

    def defer(self, data) -> None:
        """Take `data` (bytes) without parsing it yet. Parsed, in order, by
        the next read of the grid — or at once past `LAG_SYNC_BYTES`."""
        if not data:
            return
        self._lag += data
        if len(self._lag) >= LAG_SYNC_BYTES:
            self._sync()

    @property
    def lag(self) -> int:
        """Bytes deferred and not yet parsed. Tests."""
        return len(self._lag)

    def _sync(self) -> None:
        if not self._lag:
            return
        pending = bytes(self._lag)
        self._lag.clear()
        self._feed_now(pending)

    def _feed_now(self, data) -> None:
        if not data:
            return
        text = self._decoder.decode(data) if isinstance(data, (bytes, bytearray)) else str(data)
        if not text:
            return
        self.revision += 1
        for ch in text:
            self._step(ch)

    def resize(self, cols: int, rows: int) -> None:
        """Re-lay the grid: content stays top-left, nothing is rewrapped, and
        every row is dirtied so the next `since` is a full frame."""
        cols = max(MIN_COLS, min(MAX_COLS, int(cols)))
        rows = max(MIN_ROWS, min(MAX_ROWS, int(rows)))
        if cols == self.cols and rows == self.rows:
            return
        self._sync()
        self.revision += 1
        self.lines = self._relay(self.lines, cols, rows)
        if self._primary is not None:
            p_lines, px, py = self._primary
            self._primary = (self._relay(p_lines, cols, rows),
                             min(px, cols - 1), min(py, rows - 1))
        self.cols, self.rows = cols, rows
        self._top, self._bottom = 0, rows - 1
        self._x = min(self._x, cols - 1)
        self._y = min(self._y, rows - 1)
        self._pending_wrap = False
        self._floor = self.revision
        for y in range(rows):
            self._dirty[y] = self.revision

    def row_revision(self, y: int) -> int:
        self._sync()
        return self._dirty.get(y, 0)

    def since(self, revision) -> tuple:
        """`(revision, [(y, runs)], overflowed)`: the rows dirtied after the
        caller's `revision`, ordered by the revision that dirtied them and
        then by row. A revision below the floor (a resize or a screen swap
        since) or above ours (a caller from an earlier daemon life) cannot be
        served incrementally: `overflowed` is True and every row is sent.
        """
        self._sync()
        try:
            rev = int(revision)
        except (TypeError, ValueError):
            rev = -1
        full = rev < self._floor or rev > self.revision
        ys = [y for y in range(self.rows)
              if full or self._dirty.get(y, 0) > rev]
        ys.sort(key=lambda y: (self._dirty.get(y, 0), y))
        rows = [(y, runs_of(self.lines[y])) for y in ys]
        return self.revision, rows, bool(full and rev >= 0)

    def text(self) -> list:
        """Every row as a plain string, trailing blanks stripped. Tests."""
        self._sync()
        return ["".join(c[0] for c in row).rstrip() for row in self.lines]

    def history_runs(self) -> list:
        """Scrollback as wire runs, oldest first. Empty when nothing has
        scrolled off the live screen."""
        self._sync()
        return [runs_of(list(row)) for row in self._scrollback]

    def snapshot(self) -> dict:
        self._sync()
        return {
            "cols": self.cols, "rows": self.rows, "revision": self.revision,
            "cursor": list(self.cursor), "cursor_visible": self.cursor_visible,
            "title": self.title, "alt_screen": self.alt_screen,
            "bracketed_paste": self.bracketed_paste,
            "modes": sorted(self._modes),
            "rows_changed": [[y, runs_of(row)] for y, row in enumerate(self.lines)],
            "history": self.history_runs(),
        }

    def paint(self) -> bytes:
        """The screen as the byte sequence that draws it on a freshly reset
        terminal: scrollback first (so it lands in the viewer's own history),
        then every row of the primary screen, then the alternate screen if
        one is up, then the cursor, its visibility, and every DEC private
        mode the application turned on. What a viewer attaching to a running
        session is fed **instead of** the raw ring, which starts wherever
        256 KB ago happened to fall — routinely inside an escape sequence."""
        self._sync()
        out: list = ["\x1b[0m\x1b[?25l\x1b[H"]
        primary = self._primary[0] if self._primary is not None else self.lines
        if self._scrollback:
            out.append(self._rows_text(list(self._scrollback)))
            out.append("\x1b[0m\r\n")
        out.append(self._rows_text(primary))
        if self.alt_screen:
            out.append("\x1b[0m\x1b[?1049h\x1b[H")
            out.append(self._rows_text(self.lines))
        if self._top != 0 or self._bottom != self.rows - 1:
            out.append(f"\x1b[{self._top + 1};{self._bottom + 1}r")
        x, y = min(self._x, self.cols - 1), self._y
        out.append(f"\x1b[0m\x1b[{y + 1};{x + 1}H")
        out.append(self._sgr_text(self._fg, self._bg, self._attr))
        if not self.autowrap:
            out.append("\x1b[?7l")
        if self.bracketed_paste:
            out.append("\x1b[?2004h")
        for mode in sorted(self._modes):
            if mode not in _PAINTED_MODES:
                out.append(f"\x1b[?{mode}h")
        if self.title:
            out.append("\x1b]0;" + self.title.replace("\x07", "") + "\x07")
        if self.cursor_visible:
            out.append("\x1b[?25h")
        return "".join(out).encode("utf-8", "replace")

    def _rows_text(self, rows: list) -> str:
        """Rows drawn one under another, CR LF between (never after the
        last, so the cursor row is the last row drawn), attributes stated at
        each change and reset at each row's end."""
        parts: list = []
        for i, row in enumerate(rows):
            if i:
                parts.append("\x1b[0m\r\n")
            fg = bg = DEFAULT
            attr = 0
            for text, rfg, rbg, rattr in runs_of(list(row)):
                if (rfg, rbg, rattr) != (fg, bg, attr):
                    parts.append(self._sgr_text(rfg, rbg, rattr))
                    fg, bg, attr = rfg, rbg, rattr
                parts.append(text)
        return "".join(parts)

    @staticmethod
    def _sgr_text(fg: int, bg: int, attr: int) -> str:
        codes = ["0"]
        if attr & A_BOLD:
            codes.append("1")
        if attr & A_DIM:
            codes.append("2")
        if attr & A_ITALIC:
            codes.append("3")
        if attr & A_UNDERLINE:
            codes.append("4")
        if attr & A_INVERSE:
            codes.append("7")
        for base, colour in ((30, fg), (40, bg)):
            if colour == DEFAULT:
                continue
            if colour & TRUECOLOR:
                codes.append(f"{base + 8};2;{(colour >> 16) & 255};"
                             f"{(colour >> 8) & 255};{colour & 255}")
            elif 0 <= colour < 8:
                codes.append(str(base + colour))
            elif 8 <= colour < 16:
                codes.append(str(base + 60 + colour - 8))
            else:
                codes.append(f"{base + 8};5;{colour}")
        return "\x1b[" + ";".join(codes) + "m"

    def restore(self, snap: dict) -> None:
        """Rebuild from `snapshot()`. Used when a new daemon reconnects to
        a broker that kept the live terminals."""
        if not isinstance(snap, dict):
            return
        cols = max(MIN_COLS, min(MAX_COLS, int(snap.get("cols") or self.cols or 80)))
        rows = max(MIN_ROWS, min(MAX_ROWS, int(snap.get("rows") or self.rows or 24)))
        self.cols, self.rows = cols, rows
        self._top, self._bottom = 0, rows - 1
        self.lines = [self._blank_row() for _ in range(rows)]
        self._lag.clear()
        self._scrollback.clear()
        for row_runs in snap.get("history") or []:
            self._scrollback.append(self._row_from_runs(row_runs, cols))
        for item in snap.get("rows_changed") or []:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                y = int(item[0])
            except (TypeError, ValueError):
                continue
            if 0 <= y < rows:
                self.lines[y] = self._row_from_runs(item[1], cols)
        try:
            self.revision = int(snap.get("revision") or 0)
        except (TypeError, ValueError):
            self.revision = 0
        self._floor = self.revision
        self._touch_all()
        cur = snap.get("cursor") or [0, 0]
        try:
            self._x = min(max(0, int(cur[0])), cols - 1)
        except (TypeError, ValueError, IndexError):
            self._x = 0
        try:
            self._y = min(max(0, int(cur[1])), rows - 1)
        except (TypeError, ValueError, IndexError):
            self._y = 0
        self.cursor_visible = bool(snap.get("cursor_visible", True))
        self.title = str(snap.get("title") or "")
        self.alt_screen = bool(snap.get("alt_screen", False))
        self.bracketed_paste = bool(snap.get("bracketed_paste", False))
        modes = snap.get("modes")
        if isinstance(modes, list):
            self._modes = {int(m) for m in modes if isinstance(m, int)}
        self._pending_wrap = False

    def _row_from_runs(self, runs, cols: int) -> list:
        row = []
        for run in runs or []:
            if not isinstance(run, (list, tuple)) or not run:
                continue
            text = str(run[0] or "")
            try:
                fg = int(run[1]) if len(run) > 1 else DEFAULT
            except (TypeError, ValueError):
                fg = DEFAULT
            try:
                bg = int(run[2]) if len(run) > 2 else DEFAULT
            except (TypeError, ValueError):
                bg = DEFAULT
            try:
                attr = int(run[3]) if len(run) > 3 else 0
            except (TypeError, ValueError):
                attr = 0
            for ch in text:
                row.append((ch, fg, bg, attr))
                if len(row) >= cols:
                    return row
        while len(row) < cols:
            row.append((" ", DEFAULT, DEFAULT, 0))
        return row

    # ── grid helpers ─────────────────────────────────────────────────────────

    def _blank_row(self, bg: int = DEFAULT) -> list:
        return [(" ", DEFAULT, bg, 0) for _ in range(self.cols)]

    def _relay(self, lines: list, cols: int, rows: int) -> list:
        out = []
        for row in lines[:rows]:
            row = list(row[:cols])
            row.extend((" ", DEFAULT, DEFAULT, 0) for _ in range(cols - len(row)))
            out.append(row)
        while len(out) < rows:
            out.append([(" ", DEFAULT, DEFAULT, 0) for _ in range(cols)])
        return out

    def _touch(self, y: int) -> None:
        self._dirty[y] = self.revision

    def _touch_all(self) -> None:
        for y in range(self.rows):
            self._dirty[y] = self.revision

    def _erase_cell(self):
        return (" ", DEFAULT, self._bg, 0)

    def _scroll_up(self, n: int = 1) -> None:
        n = max(1, min(n, self._bottom - self._top + 1))
        for _ in range(n):
            gone = self.lines.pop(self._top)
            if (not self.alt_screen and self._top == 0
                    and self._bottom == self.rows - 1):
                self._scrollback.append(gone)
            self.lines.insert(self._bottom, self._blank_row(self._bg))
        for y in range(self._top, self._bottom + 1):
            self._touch(y)

    def _scroll_down(self, n: int = 1) -> None:
        n = max(1, min(n, self._bottom - self._top + 1))
        for _ in range(n):
            self.lines.pop(self._bottom)
            self.lines.insert(self._top, self._blank_row(self._bg))
        for y in range(self._top, self._bottom + 1):
            self._touch(y)

    def _linefeed(self) -> None:
        if self._y == self._bottom:
            self._scroll_up(1)
        elif self._y < self.rows - 1:
            self._y += 1

    def _reverse_index(self) -> None:
        if self._y == self._top:
            self._scroll_down(1)
        elif self._y > 0:
            self._y -= 1

    def _put(self, ch: str) -> None:
        w = char_width(ch)
        if w == 0:
            # A combining mark joins the cell just written: the one under the
            # cursor while a wrap is pending, else the one to its left.
            x = self._x if self._pending_wrap else self._x - 1
            if x < 0:
                return
            x = min(x, self.cols - 1)
            c = self.lines[self._y][x]
            self.lines[self._y][x] = (c[0] + ch, c[1], c[2], c[3])
            self._touch(self._y)
            return
        if self._pending_wrap:
            if self.autowrap:
                self._x = 0
                self._linefeed()
            else:
                self._x = self.cols - 1
            self._pending_wrap = False
        if w == 2 and self._x == self.cols - 1:
            # A wide glyph does not fit in the last column: pad and wrap.
            self.lines[self._y][self._x] = self._erase_cell()
            self._touch(self._y)
            if self.autowrap:
                self._x = 0
                self._linefeed()
            else:
                return
        row = self.lines[self._y]
        row[self._x] = (ch, self._fg, self._bg, self._attr)
        if w == 2:
            if self._x + 1 < self.cols:
                row[self._x + 1] = ("", self._fg, self._bg, self._attr)
        self._touch(self._y)
        self._x += w
        if self._x >= self.cols:
            self._x = self.cols - 1
            self._pending_wrap = self.autowrap
            if not self.autowrap:
                self._x = self.cols - 1

    # ── the parser ───────────────────────────────────────────────────────────

    def _step(self, ch: str) -> None:
        st = self._state
        if st == _GROUND:
            self._ground(ch)
        elif st == _ESC:
            self._escape(ch)
        elif st == _CSI:
            self._csi_byte(ch)
        elif st == _OSC:
            self._osc_byte(ch)
        elif st == _STR:
            self._string_byte(ch)
        elif st == _CHARSET:
            self._state = _GROUND

    def _control(self, ch: str) -> bool:
        """Execute a C0 control. True if `ch` was one."""
        if ch == "\r":
            self._x = 0
            self._pending_wrap = False
        elif ch in "\n\x0b\x0c":
            self._linefeed()
            self._pending_wrap = False
        elif ch == "\b":
            if self._pending_wrap:
                self._pending_wrap = False
            elif self._x > 0:
                self._x -= 1
        elif ch == "\t":
            self._pending_wrap = False
            self._x = min(self.cols - 1, (self._x // 8 + 1) * 8)
        elif ch == "\x07":
            self.bells += 1
        elif ch == "\x1b":
            self._state = _ESC
            self._buf = ""
        elif ch in "\x0e\x0f\x00\x7f":
            pass
        elif ord(ch) < 0x20:
            pass
        else:
            return False
        return True

    def _ground(self, ch: str) -> None:
        if self._control(ch):
            return
        self._put(ch)

    def _escape(self, ch: str) -> None:
        if ch == "[":
            self._state = _CSI
            self._buf = ""
        elif ch == "]":
            self._state = _OSC
            self._buf = ""
        elif ch in "PX^_":
            self._state = _STR
            self._buf = ""
        elif ch in "()*+-./#%":
            self._state = _CHARSET
        elif ch == "7":
            self._save_cursor()
            self._state = _GROUND
        elif ch == "8":
            self._restore_cursor()
            self._state = _GROUND
        elif ch == "D":
            self._linefeed()
            self._state = _GROUND
        elif ch == "M":
            self._reverse_index()
            self._state = _GROUND
        elif ch == "E":
            self._x = 0
            self._linefeed()
            self._state = _GROUND
        elif ch == "c":
            self._reset()
            self._state = _GROUND
        elif ch == "\x1b":
            self._state = _ESC
        elif ord(ch) < 0x20:
            # A control inside an escape is executed, xterm's rule.
            self._control(ch)
        else:
            # `=`, `>`, `\\` (a stray ST) and every unknown final: consumed.
            self._state = _GROUND

    def _csi_byte(self, ch: str) -> None:
        o = ord(ch)
        if 0x30 <= o <= 0x3F or 0x20 <= o <= 0x2F:
            if len(self._buf) < 64:
                self._buf += ch
            return
        if 0x40 <= o <= 0x7E:
            params, inter = self._buf, ""
            for i, c in enumerate(params):
                if 0x20 <= ord(c) <= 0x2F:
                    params, inter = params[:i], params[i:]
                    break
            self._state = _GROUND
            self._csi(params, inter, ch)
            return
        if ch == "\x1b":
            self._state = _ESC
            return
        if o < 0x20:
            self._control(ch)
            return
        self._state = _GROUND

    def _osc_byte(self, ch: str) -> None:
        if ch == "\x07":
            self._osc(self._buf)
            self._state = _GROUND
        elif ch == "\x1b":
            # ST is ESC \ — the backslash is swallowed by the ESC state.
            self._osc(self._buf)
            self._state = _ESC
        elif len(self._buf) < 4096:
            self._buf += ch

    def _string_byte(self, ch: str) -> None:
        if ch == "\x07":
            self._state = _GROUND
        elif ch == "\x1b":
            self._state = _ESC

    def _osc(self, body: str) -> None:
        code, _, arg = body.partition(";")
        if code in ("0", "2"):
            self.title = arg[:200]

    # ── CSI dispatch ─────────────────────────────────────────────────────────

    @staticmethod
    def _ints(params: str) -> list:
        out = []
        for p in params.split(";"):
            head = p.split(":", 1)[0]
            try:
                out.append(int(head) if head else 0)
            except ValueError:
                out.append(0)
        return out

    def _csi(self, params: str, inter: str, final: str) -> None:
        private = params[:1] in ("?", ">", "<", "=")
        if private:
            mark, params = params[0], params[1:]
            if mark == "?" and final in "hl":
                for mode in self._ints(params):
                    self._dec_mode(mode, final == "h")
            return
        if inter:
            return
        n = self._ints(params)
        p0 = n[0] if n else 0
        one = max(1, p0)
        if final == "A":
            self._move(-one, 0)
        elif final == "B":
            self._move(one, 0)
        elif final == "C":
            self._x = min(self.cols - 1, self._x + one)
            self._pending_wrap = False
        elif final == "D":
            self._x = max(0, self._x - one)
            self._pending_wrap = False
        elif final == "E":
            self._move(one, 0)
            self._x = 0
        elif final == "F":
            self._move(-one, 0)
            self._x = 0
        elif final == "G":
            self._x = max(0, min(self.cols - 1, one - 1))
            self._pending_wrap = False
        elif final in "Hf":
            row = max(1, n[0] if n else 1)
            col = max(1, n[1] if len(n) > 1 else 1)
            self._y = min(self.rows - 1, row - 1)
            self._x = min(self.cols - 1, col - 1)
            self._pending_wrap = False
        elif final == "d":
            self._y = max(0, min(self.rows - 1, one - 1))
            self._pending_wrap = False
        elif final == "J":
            self._erase_display(p0)
        elif final == "K":
            self._erase_line(p0)
        elif final == "L":
            self._insert_lines(one)
        elif final == "M":
            self._delete_lines(one)
        elif final == "@":
            self._insert_chars(one)
        elif final == "P":
            self._delete_chars(one)
        elif final == "X":
            self._erase_chars(one)
        elif final == "S":
            self._scroll_up(one)
        elif final == "T":
            self._scroll_down(one)
        elif final == "r":
            top = max(1, n[0] if n else 1)
            bottom = n[1] if len(n) > 1 and n[1] else self.rows
            bottom = min(self.rows, bottom)
            if top < bottom:
                self._top, self._bottom = top - 1, bottom - 1
                self._x, self._y = 0, 0
                self._pending_wrap = False
        elif final == "m":
            self._sgr(params)
        elif final == "s":
            self._save_cursor()
        elif final == "u":
            self._restore_cursor()
        # `n` (DSR), `c` (DA), `t` (window ops) and the rest: consumed.

    def _move(self, dy: int, _dx: int) -> None:
        self._pending_wrap = False
        if dy < 0:
            floor = self._top if self._y >= self._top else 0
            self._y = max(floor, self._y + dy)
        else:
            ceil = self._bottom if self._y <= self._bottom else self.rows - 1
            self._y = min(ceil, self._y + dy)

    def _dec_mode(self, mode: int, on: bool) -> None:
        if on:
            self._modes.add(mode)
        else:
            self._modes.discard(mode)
        if mode == 25:
            self.cursor_visible = on
        elif mode == 7:
            self.autowrap = on
            self._pending_wrap = False
        elif mode == 2004:
            self.bracketed_paste = on
        elif mode in (1049, 1047, 47):
            if on and not self.alt_screen:
                if mode == 1049:
                    self._save_cursor()
                self._primary = (self.lines, self._x, self._y)
                self.lines = [self._blank_row() for _ in range(self.rows)]
                self.alt_screen = True
                self._x = self._y = 0
                self._pending_wrap = False
                self._floor = self.revision
                self._touch_all()
            elif not on and self.alt_screen:
                lines, x, y = self._primary or (None, 0, 0)
                self._primary = None
                self.lines = lines if lines is not None else [
                    self._blank_row() for _ in range(self.rows)]
                self.alt_screen = False
                self._x, self._y = x, y
                if mode == 1049:
                    self._restore_cursor()
                self._pending_wrap = False
                self._floor = self.revision
                self._touch_all()

    def _erase_display(self, p: int) -> None:
        if p == 0:
            self._erase_line(0)
            for y in range(self._y + 1, self.rows):
                self.lines[y] = self._blank_row(self._bg)
                self._touch(y)
        elif p == 1:
            self._erase_line(1)
            for y in range(0, self._y):
                self.lines[y] = self._blank_row(self._bg)
                self._touch(y)
        elif p == 2:
            for y in range(self.rows):
                self.lines[y] = self._blank_row(self._bg)
                self._touch(y)
        elif p == 3:
            self._scrollback.clear()

    def _erase_line(self, p: int) -> None:
        row = self.lines[self._y]
        x = min(self._x, self.cols - 1)
        if p == 0:
            rng = range(x, self.cols)
        elif p == 1:
            rng = range(0, x + 1)
        elif p == 2:
            rng = range(0, self.cols)
        else:
            return
        for i in rng:
            row[i] = self._erase_cell()
        self._touch(self._y)
        self._pending_wrap = False

    def _insert_lines(self, n: int) -> None:
        if not (self._top <= self._y <= self._bottom):
            return
        n = min(n, self._bottom - self._y + 1)
        for _ in range(n):
            self.lines.pop(self._bottom)
            self.lines.insert(self._y, self._blank_row(self._bg))
        for y in range(self._y, self._bottom + 1):
            self._touch(y)
        self._x = 0
        self._pending_wrap = False

    def _delete_lines(self, n: int) -> None:
        if not (self._top <= self._y <= self._bottom):
            return
        n = min(n, self._bottom - self._y + 1)
        for _ in range(n):
            self.lines.pop(self._y)
            self.lines.insert(self._bottom, self._blank_row(self._bg))
        for y in range(self._y, self._bottom + 1):
            self._touch(y)
        self._x = 0
        self._pending_wrap = False

    def _insert_chars(self, n: int) -> None:
        row = self.lines[self._y]
        x = min(self._x, self.cols - 1)
        n = min(n, self.cols - x)
        del row[self.cols - n:]
        for _ in range(n):
            row.insert(x, self._erase_cell())
        self._touch(self._y)
        self._pending_wrap = False

    def _delete_chars(self, n: int) -> None:
        row = self.lines[self._y]
        x = min(self._x, self.cols - 1)
        n = min(n, self.cols - x)
        del row[x:x + n]
        row.extend(self._erase_cell() for _ in range(n))
        self._touch(self._y)
        self._pending_wrap = False

    def _erase_chars(self, n: int) -> None:
        row = self.lines[self._y]
        x = min(self._x, self.cols - 1)
        for i in range(x, min(self.cols, x + n)):
            row[i] = self._erase_cell()
        self._touch(self._y)
        self._pending_wrap = False

    def _save_cursor(self) -> None:
        self._saved_cursor = (self._x, self._y, self._fg, self._bg, self._attr)

    def _restore_cursor(self) -> None:
        x, y, fg, bg, attr = self._saved_cursor
        self._x = min(x, self.cols - 1)
        self._y = min(y, self.rows - 1)
        self._fg, self._bg, self._attr = fg, bg, attr
        self._pending_wrap = False

    def _reset(self) -> None:
        self.lines = [self._blank_row() for _ in range(self.rows)]
        self._x = self._y = 0
        self._top, self._bottom = 0, self.rows - 1
        self._fg = self._bg = DEFAULT
        self._attr = 0
        self._pending_wrap = False
        self.cursor_visible = True
        self.autowrap = True
        self.bracketed_paste = False
        self.alt_screen = False
        self._primary = None
        self._modes.clear()
        self._floor = self.revision
        self._touch_all()

    def _sgr(self, params: str) -> None:
        if params == "":
            self._fg = self._bg = DEFAULT
            self._attr = 0
            return
        items: list = []
        for p in params.split(";"):
            sub = p.split(":")
            try:
                items.append([int(s) if s else 0 for s in sub])
            except ValueError:
                items.append([0])
        i = 0
        while i < len(items):
            item = items[i]
            code = item[0]
            i += 1
            if code == 0:
                self._fg = self._bg = DEFAULT
                self._attr = 0
            elif code == 1:
                self._attr |= A_BOLD
            elif code == 2:
                self._attr |= A_DIM
            elif code == 3:
                self._attr |= A_ITALIC
            elif code == 4:
                self._attr |= A_UNDERLINE
            elif code == 7:
                self._attr |= A_INVERSE
            elif code == 22:
                self._attr &= ~(A_BOLD | A_DIM)
            elif code == 23:
                self._attr &= ~A_ITALIC
            elif code == 24:
                self._attr &= ~A_UNDERLINE
            elif code == 27:
                self._attr &= ~A_INVERSE
            elif code == 39:
                self._fg = DEFAULT
            elif code == 49:
                self._bg = DEFAULT
            elif 30 <= code <= 37:
                self._fg = code - 30
            elif 40 <= code <= 47:
                self._bg = code - 40
            elif 90 <= code <= 97:
                self._fg = code - 90 + 8
            elif 100 <= code <= 107:
                self._bg = code - 100 + 8
            elif code in (38, 48):
                # `38;5;n` / `38;2;r;g;b` as separate params, or the colon
                # form `38:5:n` / `38:2::r:g:b` inside one.
                if len(item) > 1:
                    args = item[1:]
                else:
                    args = []
                    if i < len(items):
                        args.append(items[i][0])
                        i += 1
                        if args[0] == 5 and i < len(items):
                            args.append(items[i][0])
                            i += 1
                        elif args[0] == 2:
                            while len(args) < 4 and i < len(items):
                                args.append(items[i][0])
                                i += 1
                colour = self._extended_colour(args)
                if colour is not None:
                    if code == 38:
                        self._fg = colour
                    else:
                        self._bg = colour
            # Anything else (blink, conceal, fonts, framed): ignored.

    @staticmethod
    def _extended_colour(args: list) -> Optional[int]:
        if not args:
            return None
        if args[0] == 5 and len(args) >= 2:
            return max(0, min(255, args[1]))
        if args[0] == 2:
            rgb = [a for a in args[1:]]
            # The colon form may carry a colour-space id: `2::r:g:b`.
            if len(rgb) >= 4:
                rgb = rgb[1:4]
            if len(rgb) >= 3:
                r, g, b = (max(0, min(255, v)) for v in rgb[:3])
                return TRUECOLOR | (r << 16) | (g << 8) | b
        return None


__all__ = ["Screen", "runs_of", "char_width", "DEFAULT", "TRUECOLOR",
           "A_BOLD", "A_DIM", "A_UNDERLINE", "A_INVERSE", "A_ITALIC",
           "SCROLLBACK_LINES", "LAG_SYNC_BYTES", "MIN_COLS", "MAX_COLS",
           "MIN_ROWS", "MAX_ROWS"]
