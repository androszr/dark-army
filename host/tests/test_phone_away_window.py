# host/tests/test_phone_away_window.py
"""The phone's half of the away window: it draws the Mac's number and
derives nothing.

How long a check-in at home buys is a grant made on the Mac, stored on the
relay channel entry and deliberately **not** on the wire. The phone is given
one thing — ``lease_expires_at`` — and formats it in exactly one place, so
the top bar and the profile screen can never disagree and no later change can
quietly hand the away side a length it could argue with.

Grep pins over ``ios/BobPhone/*.swift``, the ``test_phone_*.py`` family's
shape: the phone target is not built here, so these are the seam that keeps
the properties true between Xcode runs. The arithmetic itself is executable,
in ``ios/BobPhoneTests/AwayWindowTests.swift``.
"""

from __future__ import annotations

import pathlib

IOS = pathlib.Path(__file__).resolve().parents[2] / "ios"


def _swift_files() -> list[pathlib.Path]:
    files = sorted((IOS / "BobPhone").rglob("*.swift"))
    assert files, "the phone's sources moved"
    return files


def test_the_phone_knows_nothing_of_a_grant():
    """`lease_days` is the Mac's word. Nothing under `ios/` may name it: a
    phone that could read the length is one step from a phone that argues
    about it."""
    hits = [p for p in sorted(IOS.rglob("*.swift"))
            if "lease_days" in p.read_text()]
    assert hits == []


def test_the_expiry_is_read_in_exactly_four_places():
    """The snapshot's model, the client that mirrors it, and the two screens
    that draw it. A fifth reader would be a second derivation."""
    named = {p.name for p in _swift_files()
             if "leaseExpiresAt" in p.read_text()}
    assert named == {"Models.swift", "Client.swift", "BrandBar.swift",
                     "ProfileView.swift"}


def test_both_away_surfaces_go_through_one_formatter():
    for name in ("BrandBar.swift", "ProfileView.swift"):
        body = (IOS / "BobPhone" / name).read_text()
        assert "AwaySpan.line(" in body, name


def test_the_final_day_threshold_is_named_once():
    """One constant, in `AwaySpan`. A second copy is a boundary that can
    drift away from the one the tests pin."""
    declarations = 0
    for path in _swift_files():
        declarations += path.read_text().count("static let finalDay")
    assert declarations == 1


def test_the_lapsed_sentence_is_unchanged():
    """It mirrors the Mac's own `LEASE_REFUSAL` and is the one line here a
    person may already have read; the length becoming a choice does not
    reword it."""
    body = (IOS / "BobPhone" / "BrandBar.swift").read_text()
    assert ("away access lapsed — check in on home Wi-Fi to renew it"
            in body)
