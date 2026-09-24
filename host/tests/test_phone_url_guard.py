"""Static guard: nothing in the phone app force-unwraps.

`Pairing.swift` once built its pairing URL with `URL(string: ...)!`, which is
nil for the everyday typed address — a space in it — so the app died on the one
pairing route the simulator has. The fix routes every address through
`HostAddress.check`; this is the standing check that no `)!`, `try!` or `as!`
comes back into `ios/BobPhone/`.

It is a *lint*, not a behavioural test: the phone app has no test target by
explicit decision, and the checker's own rules are exercised in Swift over in
`panel/Tests/BobPanelTests/HostAddressTests.swift`.
"""

from pathlib import Path

import pytest

PHONE = Path(__file__).resolve().parents[2] / "ios" / "BobPhone"

# The three ways Swift traps at runtime on a value it was handed. Whole-line
# comments are skipped — prose about the old crash lives in these files' own
# doc comments — but a trailing comment is *not* cut off the line, because a
# URL in the code carries `//` of its own and cutting there would blind the
# scan to the very line it exists to catch.
BANNED = (")!", "try!", "as!")

# The literal line that used to kill the app. The self-test below feeds it
# through the same scan, so a future "cleanup" of the rules above cannot quietly
# turn this file into a vacuous pass.
OLD_CRASH_LINE = '            url: URL(string: "http://\\(trimmedHost):\\(port)/api/pair")!)'


def _offences(text: str) -> list[tuple[int, str]]:
    hits = []
    for number, line in enumerate(text.splitlines(), start=1):
        code = line.strip()
        if code.startswith("//") or code.startswith("///"):
            continue
        if any(token in code for token in BANNED):
            hits.append((number, line.strip()))
    return hits


def test_the_phone_app_source_is_there():
    assert PHONE.is_dir(), f"{PHONE} is missing — this guard must never silently skip"
    assert list(PHONE.glob("*.swift")), "no Swift sources under ios/BobPhone"


@pytest.mark.parametrize("path", sorted(PHONE.glob("*.swift")), ids=lambda p: p.name)
def test_no_force_unwrap_in_the_phone_app(path):
    hits = _offences(path.read_text())
    assert not hits, (
        f"{path.name} force-unwraps: "
        + "; ".join(f"line {n}: {line}" for n, line in hits)
        + " — refuse in words instead (see HostAddress.swift)"
    )


def test_the_scan_catches_the_line_it_was_written_for():
    assert _offences(OLD_CRASH_LINE) == [(1, OLD_CRASH_LINE.strip())]
