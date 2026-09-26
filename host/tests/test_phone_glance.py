# host/tests/test_phone_glance.py
"""Four phone rules from the 20 Sep 2026 usability audit, pinned in source.

* A glance does not cost a reconnect: `.inactive` locks the screen and
  leaves the poller running; `.background` pauses it (`suspend()`, a
  departure stamp — the poller stops itself past `BackgroundGrace.window`);
  the unlock path keeps a running poller inside the window (`wake()`) and
  starts fresh otherwise (`test_phone_background_grace.py`).
* Keys typed into a terminal from away are batched into one relay write
  per line or pause (`AwayKeys`), and a keystroke drags no state frame.
* Needs you has **Dismiss all** — the Mac's `Inbox.dismissable`, `inbox_ack` per
  row, armed against the exact set.
* A photo picked away is kept on the phone and the card is banked, never
  refused; the outbox reads the home-only refusal as "wait".
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IOS = ROOT / "ios" / "BobPhone"


def _read(name: str) -> str:
    return (IOS / name).read_text()


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


# --- a glance is not a reconnect --------------------------------------------

def test_inactive_locks_without_stopping_the_poller():
    app = _read("BobPhoneApp.swift")
    inactive = app.split("case .inactive:")[1].split("case .background:")[0]
    assert "lock.lock()" in inactive
    assert "client.stop()" not in inactive
    background = app.split("case .background:")[1].split("@unknown default:")[0]
    assert "lock.lock()" in background and "client.suspend()" in background
    assert "client.stop()" not in background


def test_the_unlock_path_restarts_only_a_stopped_poller():
    app = _read("BobPhoneApp.swift")
    gate = app[app.index(".onChange(of: lock.unlocked)"):]
    gate = gate[:gate.index(".onChange(of: pairing.record)")]
    # One decision, `BackgroundGrace.verdict`: keep the running poller and
    # ask for a check-in now, or start fresh (nothing running, or back
    # past the window).
    assert "BackgroundGrace.verdict(" in gate
    assert "client.wake()" in gate
    assert "client.start(record: record)" in gate
    assert "if !client.isPolling" not in gate
    # Locking no longer stops the client from this hook: the one mention
    # left is a comment about the `.active` ordering.
    code = "\n".join(l for l in gate.splitlines() if not l.strip().startswith("//"))
    assert "client.stop()" not in code
    client = _read("Client.swift")
    assert "var isPolling: Bool { task != nil }" in client


# --- away keys are batched -----------------------------------------------------

def test_away_keys_flush_on_a_committing_key_or_a_pause_under_the_budget():
    """26 Sep 2026: Backspace and arrows no longer spend a write each; only
    Enter, Ctrl-C, Ctrl-D and a lone Escape send at once. The phone mirrors
    the Mac's key bucket and holds a batch it would refuse, and a refusal
    that happens anyway puts the keys back — none are dropped for the rate."""
    import re
    from dark_army_daemon import relay, relay_client
    pane = _read("TerminalPane.swift")
    rule = _block(pane, "enum AwayKeys")
    assert "static let pause: TimeInterval = 0.8" in rule
    assert "$0 == 0x0D || $0 == 0x0A || $0 == 0x03 || $0 == 0x04" in rule
    assert "$0 == 0x7F" not in rule.split("static func flushesAtOnce", 1)[1].split("}", 2)[0]
    per = re.search(r"static let perMinute = (\d+)", rule)
    assert per and int(per.group(1)) == relay.RELAY_MAX_KEY_WRITES_PER_MINUTE
    assert f'static let slowDown = "{relay_client._WRITE_LIMIT_REFUSAL}"' in rule
    send = _block(pane, "func send(source: TerminalView, data: ArraySlice<UInt8>)")
    assert "AwayKeys.flushesAtOnce(data)" in send
    assert "AwayKeys.pause" in send
    assert "showPending()" in send
    flush = _block(pane, "private func flushAway()")
    assert "refreshAfter: false" in flush
    assert "awayBudget.wait()" in flush and "awayBudget.take()" in flush
    # A refused batch goes back in front, in order, and is retried.
    assert "self.pendingAway = payload + self.pendingAway" in flush
    assert "result.detail == AwayKeys.slowDown" in flush
    assert "AwayKeys.hint" in pane
    assert "Text(pending)" in pane


# --- clear all ------------------------------------------------------------------

def test_dismiss_all_covers_exactly_the_macs_dismissable_rows():
    """One verb, one meaning, on both surfaces: every entry but the
    permission ask, through `inbox_ack` — never the old `dismiss`."""
    inbox = _read("Inbox.swift")
    rule = _block(inbox, "static func dismissable(_ items: [PhoneInboxItem])")
    assert "$0.wire.dismissable" in rule
    assert "var dismissable: Bool { self != .permission }" in inbox
    mac = (ROOT / "panel" / "Sources" / "BobPanel" / "Inbox.swift").read_text()
    assert "var dismissable: Bool { self != .permission }" in mac
    view = _read("NeedsYouView.swift")
    row = _block(view, "private var dismissAllRow: some View")
    assert "PhoneInbox.dismissable(snapshot.decisionItems)" in row
    assert "dismissAllArmed == ids" in row
    assert "if dismissAllArmed != fresh { dismissAllArmed = [] }" in row
    clear = _block(view, "private func dismissAll(_ rows: [PhoneInboxItem])")
    assert "PhoneActions.inboxAck" in clear
    assert "PhoneActions.dismiss" not in view
    # A press is queued, not awaited (20 Sep 2026): each row's ack is
    # written into the queue under its own subject, and no refresh is
    # forced behind it.
    assert "client.enqueue(" in clear
    assert "refreshAfterWrite" not in clear


# --- photos away are kept, not refused --------------------------------------

def test_a_photo_picked_away_stays_on_the_phone_and_banks_the_card():
    composer = _read("ComposerView.swift")
    assert "let local = offline || client.knowsItIsAway" in composer
    assert "if offline || !localPhotos.isEmpty {\n            bank()" in composer
    outbox = _read("Outbox.swift")
    sentences = _block(outbox, "static let transportSentences: Set<String>")
    assert "PhoneActions.photosNeedHome" in sentences


# --- the lease reminder -------------------------------------------------------------

def test_the_lease_reminder_follows_the_published_expiry():
    reminder = _read("LeaseReminder.swift")
    assert 'static let identifier = "lease-reminder"' in reminder
    assert "static let lead: TimeInterval = 12 * 60 * 60" in reminder
    assert "removePendingNotificationRequests(withIdentifiers: [identifier])" in reminder
    for stranger in ("URLSession", "RelayChannel", "HomeChannel", "PhoneClient"):
        assert stranger not in reminder, stranger
    client = _read("Client.swift")
    lease = _block(client, "@Published var leaseExpiresAt: Double = 0 {")
    assert "LeaseReminder.sync(expiresAt: leaseExpiresAt)" in lease
    forget = _block(client, "private func forgetPairing()")
    assert "leaseExpiresAt = 0" in forget


# --- the widget's face opens its agent ----------------------------------------

def test_the_widget_face_links_to_its_agent_by_one_builder():
    shared = (ROOT / "ios" / "Shared" / "FleetSummary.swift").read_text()
    assert "var sessionId = \"\"" in shared
    assert 'sessionId = try c.decodeIfPresent(String.self, forKey: .sessionId) ?? ""' in shared
    links = _block(shared, "enum FleetLinks")
    assert 'parts.host = "fleet"' in links
    assert 'URLQueryItem(name: "session", value: sessionId)' in links
    widget = (ROOT / "ios" / "BobPhoneWidget" / "WidgetViews.swift").read_text()
    assert "FleetLinks.agent(face.sessionId)" in widget
    assert "bobphone://fleet" not in widget  # composed, never spelled
    router = _read("Router.swift")
    assert 'first { $0.name == "session" }' in router
    assert "func takeSession() -> String" in router
    app = _read("BobPhoneApp.swift")
    pending = _block(app, "private func applyPendingTab()")
    assert "router.takeSession()" in pending
    assert "PhoneInbox.uniqueAgent(" in pending
