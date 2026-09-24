# host/tests/test_phone_held_picture.py
"""The phone keeps the last picture it saw, and a restore is not evidence.

Structural pins over `ios/BobPhone/HeldPicture.swift`, `Client.swift`,
`BobPhoneApp.swift` and `BrandBar.swift`; the store's behaviour over a temp
folder is `HeldPictureTests.swift` in Xcode.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IOS = ROOT / "ios" / "BobPhone"
STORE = IOS / "HeldPicture.swift"
CLIENT = IOS / "Client.swift"
APP = IOS / "BobPhoneApp.swift"
BAR = IOS / "BrandBar.swift"


def _block(text: str, start: str) -> str:
    i = text.index(start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    raise AssertionError(start)


def test_the_store_writes_protected_and_off_the_main_actor():
    text = STORE.read_text()
    remember = _block(text, "func remember(body:")
    assert "Task.detached" in remember
    assert "[.atomic]" in remember
    # The protection class is set after the write and never fails it: the
    # simulator's temporary folder refuses it, and CI runs the store there.
    assert "FileProtectionType.complete" in remember
    assert "try? FileManager.default.setAttributes" in remember
    assert text.count('"held-picture.json"') == 1
    assert "@MainActor" in text
    assert "JSONDecoder().decode(Snapshot" not in text  # the client decodes


def test_the_client_remembers_only_on_a_foreground_applied_frame():
    text = CLIENT.read_text()
    apply = _block(text, "private func applyState(")
    assert apply.count("heldPicture.remember(") == 1
    guarded = apply[apply.index("if !backgroundRun {"):]
    assert "heldPicture.remember(" in _block(guarded, "if !backgroundRun {")
    assert apply.index("heldStateDigest = decoded.stateDigest") \
        < apply.index("heldPicture.remember(")


def test_a_restore_touches_nothing_live():
    text = CLIENT.read_text()
    restore = _block(text, "private func restoreHeldPicture(")
    for forbidden in ("heldStateDigest", "lastFullState", "publishWidgetSummary",
                      "onLive", "status =", "lastHeard", "leaseExpiresAt",
                      "receipts.", "mergeDoneArchive"):
        assert forbidden not in restore, forbidden
    assert "snapshot.generatedAt == 0" in restore
    assert "JSONDecoder().decode(Snapshot.self" in restore


def test_the_pairing_scopes_the_picture():
    text = CLIENT.read_text()
    start = _block(text, "func start(record: PairingRecord)")
    assert start.index("heldPicture.adopt(record.token)") \
        < start.index("restoreHeldPicture(record.token)")
    assert start.index("cardCache.adopt(record.token)") \
        < start.index("heldPicture.adopt(record.token)")
    forget = _block(text, "private func forgetPairing()")
    assert "heldPicture.forget()" in forget
    assert "pictureAsOf = nil" in forget


def test_the_picture_is_loaded_behind_the_gate_and_drawn_beside_the_banners():
    app = APP.read_text()
    gate = app[app.index(".onChange(of: lock.unlocked)"):]
    assert "client.heldPicture.load()" in gate[:1500]
    assert "HeldPictureBanner(asOf: asOf," in app
    assert "reaching: client.status != .unreachable" in app
    assert "StaleBanner(lastHeard: heard)" in app
    assert "ReconnectBar(" in app
    held = _block(app, "private var heldSince: Date?")
    assert "client.status != .live" in held
    assert "generatedAt != 0" in held
    bar = BAR.read_text()
    banner = _block(bar, "struct HeldPictureBanner")
    assert "FleetAge.text(" in banner
    assert "FleetAge.spoken(" in banner
    assert "TimelineView(.periodic" in banner


def test_the_widget_never_reads_the_held_picture():
    """Nor the notification log, nor the held-destination resolver: the
    widget's diet is `FleetSummary` alone, and the background run never
    touches a `.completeFileProtection` file."""
    for path in (ROOT / "ios" / "BobPhoneWidget").glob("*.swift"):
        text = path.read_text()
        for name in ("HeldPicture", "NotificationLog", "HeldDestination",
                     "ConversationCache", "ConversationView"):
            assert name not in text, (path.name, name)
    background = (IOS / "BackgroundRefresh.swift").read_text()
    for name in ("HeldPicture", "NotificationLog", "HeldDestination",
                 "ConversationCache", "ConversationView"):
        assert name not in background, name
