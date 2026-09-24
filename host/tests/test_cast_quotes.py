"""Every portrait slug has one quote, and only a large portrait draws it.

`identity.QUOTES` is the table; `CastQuotes` at the end of both clients'
`Cast.swift` carries the same pairs, byte-equal from `enum CastQuotes {` to
the end of the file. "Large portraits only" is a claim about *who calls it*,
so it is pinned here as the set of source files that name `CastQuotes` —
the seam that proves it without a screen.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dark_army_daemon import identity

ROOT = Path(__file__).resolve().parents[2]
PANEL_CAST = ROOT / "panel" / "Sources" / "BobPanel" / "Cast.swift"
PHONE_CAST = ROOT / "ios" / "BobPhone" / "Cast.swift"
MARKER = "enum CastQuotes {"
ROSTER = [n.lower() for n in identity.NAMES] + list(identity.ART_ONLY)

_PAIR = re.compile(r'^        "((?:[^"\\]|\\.)*)": "((?:[^"\\]|\\.)*)",$', re.M)


def _unescape(text: str) -> str:
    return text.replace('\\"', '"').replace("\\\\", "\\")


def _region(path: Path) -> str:
    """The pinned region: the marker line through end of file."""
    text = path.read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(r"^" + re.escape(MARKER) + r"$",
                                             text, re.M)]
    assert len(starts) == 1, path
    return text[starts[0]:]


def _swift_lines(region: str) -> dict:
    body = region[:region.index("\n    ]\n")]
    return {_unescape(k): _unescape(v) for k, v in _PAIR.findall(body)}


# ── the table ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("slug", ROSTER)
def test_every_roster_slug_has_a_quote(slug):
    quote = identity.QUOTES.get(slug, "")
    assert quote.strip() == quote and quote
    assert identity.quote_for(slug) == quote


def test_the_table_is_the_roster_in_roster_order():
    assert list(identity.QUOTES) == ROSTER
    assert len(identity.QUOTES) == 21


@pytest.mark.parametrize("slug", ["", "grok", "elliot", "mrrobot", "nobody-at-all"])
def test_a_slug_off_the_roster_has_no_quote(slug):
    assert identity.quote_for(slug) == ""


# ── both clients carry the same table, byte for byte ────────────────────────

@pytest.mark.parametrize("path", [PANEL_CAST, PHONE_CAST], ids=["panel", "phone"])
def test_the_swift_table_is_the_python_table(path):
    assert _swift_lines(_region(path)) == identity.QUOTES


def test_the_two_clients_are_byte_equal_from_the_marker():
    assert _region(PANEL_CAST) == _region(PHONE_CAST)


def test_a_doctored_copy_would_not_compare_equal():
    """Without this, a refactor that moved the marker or emptied the region
    would make the byte pin above pass vacuously."""
    panel = _region(PANEL_CAST)
    doctored = panel.replace("Root the plan", "Root the tree", 1)
    assert doctored != panel
    assert doctored != _region(PHONE_CAST)
    assert _swift_lines(doctored) != identity.QUOTES


# ── the Swift answers, run rather than read ─────────────────────────────────

CASES = [
    ("Cipher-ab12", identity.QUOTES["cipher"]),     # overflow keeps its stem
    ("PTYŚ", identity.QUOTES["ptyś"]),              # case-folds the Ś
    ("overwatch", identity.QUOTES["overwatch"]),     # art-only speaks
    ("Grok", ""),                                    # off the roster: silent
    ("Elliot", ""),                                  # a retired name: silent
]


@pytest.mark.parametrize("path", [PANEL_CAST, PHONE_CAST], ids=["panel", "phone"])
def test_line_for_nickname_under_swiftc(tmp_path, path):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    source = path.read_text(encoding="utf-8")
    names = re.search(r"    static let names = \[.*?\]", source, re.S)
    art = re.search(r"    static let artOnly = \[.*?\]", source, re.S)
    assert names and art, path
    harness = (
        "import Foundation\n"
        "enum Cast {\n" + names.group() + "\n" + art.group() + "\n}\n"
        + _region(path) + "\n"
        "for nickname in CommandLine.arguments.dropFirst() {\n"
        "    print(\"[\" + CastQuotes.line(forNickname: nickname) + \"]\")\n"
        "}\n")
    probe = tmp_path / "QuoteProbe.swift"
    executable = tmp_path / "QuoteProbe"
    probe.write_text(harness, encoding="utf-8")
    built = subprocess.run([swiftc, str(probe), "-o", str(executable)],
                           capture_output=True, text=True, timeout=120)
    assert built.returncode == 0, built.stderr
    ran = subprocess.run([str(executable), *[n for n, _ in CASES]],
                         capture_output=True, text=True, timeout=10)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.splitlines() == [f"[{want}]" for _, want in CASES]


# ── only a large portrait draws it ──────────────────────────────────────────

def _callers(tree: Path, skip: tuple[str, ...] = ()) -> set:
    return {str(p.relative_to(tree)) for p in tree.rglob("*.swift")
            if not any(part in skip for part in p.parts)
            and re.search(r"\bCastQuotes\b", p.read_text(encoding="utf-8"))}


def test_the_mac_draws_a_quote_only_on_its_large_portraits():
    """The detail header (76pt) and the two quiet screens, and nothing else:
    no row, tile, badge, inbox entry, crew band, area grid or card."""
    assert _callers(ROOT / "panel" / "Sources" / "BobPanel") == {
        "Cast.swift", "AgentDetailPane.swift", "PanelView.swift"}


def test_the_phone_draws_a_quote_only_on_its_agent_page():
    """`AgentDetailView`'s two 160pt portraits; never a widget, list or card."""
    assert _callers(ROOT / "ios", skip=("BobPhoneTests", "BobPhoneWidgetTests")) == {
        "BobPhone/Cast.swift", "BobPhone/AgentDetailView.swift"}


def test_every_quote_fits_one_line_beside_the_phone_still():
    # The phone draws the quote beside a 96pt still at 10pt monospace, one
    # line: about 39 characters fit on the narrowest phone before the
    # shrink-to-fit safety net starts. Keep every line inside that.
    for slug, quote in identity.QUOTES.items():
        assert len(quote) <= 39, f"{slug}: {len(quote)} chars: {quote!r}"
