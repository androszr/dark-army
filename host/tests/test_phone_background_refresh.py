# host/tests/test_phone_background_refresh.py
"""The tile keeps moving with the app closed — source pins over the phone.

`ios/` has no test target by decision; this suite reads the Swift as text.
What it pins: the app, never the widget, owns the network (`test_phone_widget`
keeps the widget clean); the background task is registered before launch
finishes, permitted in the plist, and booked on every departure; the
check-in is one bounded pass that goes the way the last poll went; and the
Keychain item is readable with the screen off, moved across in place.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
REFRESH = PHONE / "BackgroundRefresh.swift"
CLIENT = PHONE / "Client.swift"
PAIRING = PHONE / "Pairing.swift"
PUSH = PHONE / "Push.swift"
APP = PHONE / "BobPhoneApp.swift"
PLIST = PHONE / "Info.plist"
PROFILE = PHONE / "ProfileView.swift"
SUMMARY = ROOT / "ios" / "Shared" / "FleetSummary.swift"
WIDGET = ROOT / "ios" / "BobPhoneWidget" / "BobPhoneWidget.swift"
WIDGET_VIEWS = ROOT / "ios" / "BobPhoneWidget" / "WidgetViews.swift"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"

# The permitted id is spelled from the one identity setting
# (ios/Config/Identity.xcconfig); the code derives the same string from
# the running bundle id, which is that setting for the app target.
TASK_ID = "$(DARK_ARMY_BUNDLE_ID).refresh"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _body(text: str, signature: str) -> str:
    start = text.index(signature)
    rest = text[start + len(signature):]
    ends = [i for i in (rest.find("\n    func "), rest.find("\n    private func "),
                        rest.find("\n    static func "),
                        rest.find("\n    private static func "))
            if i >= 0]
    return rest[:min(ends)] if ends else rest


# --- registered, permitted, booked -------------------------------------------


def test_the_task_id_is_one_string_in_code_and_in_the_plist():
    refresh = _read(REFRESH)
    assert ('static let taskId = (Bundle.main.bundleIdentifier ?? "") + ".refresh"'
            in refresh)
    assert 'PRODUCT_BUNDLE_IDENTIFIER = "$(DARK_ARMY_BUNDLE_ID)";' in _read(PBXPROJ)
    plist = _read(PLIST)
    assert "BGTaskSchedulerPermittedIdentifiers" in plist
    assert f"<string>{TASK_ID}</string>" in plist
    assert "<key>UIBackgroundModes</key>" in plist
    assert "<string>fetch</string>" in plist


def test_the_handler_is_registered_before_launch_finishes():
    """`BGTaskScheduler.register` after `didFinishLaunching` returns is a
    crash on iOS; the one seat is the app delegate's own launch hook."""
    push = _read(PUSH)
    launch = push[push.index("didFinishLaunchingWithOptions"):]
    launch = launch[:launch.index("return true")]
    assert "BackgroundRefresh.register()" in launch


def test_every_departure_books_the_next_run():
    app = _read(APP)
    background = app[app.index("case .background:"):app.index("@unknown default:")]
    assert "BackgroundRefresh.schedule()" in background
    # And each run books its successor first, so a killed run leaves a floor.
    handle = _body(_read(REFRESH), "private static func handle(")
    assert handle.index("schedule()") < handle.index("PairingStore()")


def test_the_file_is_compiled_into_the_app_and_not_the_widget():
    pbx = _read(PBXPROJ)
    assert pbx.count("BackgroundRefresh.swift in Sources") == 2  # decl + phase
    widget_phase = pbx[pbx.index("7B0B0E1A0000000000000131 /* Sources */"):]
    widget_phase = widget_phase[:widget_phase.index("runOnlyForDeploymentPostprocessing")]
    assert "BackgroundRefresh" not in widget_phase
    assert "BackgroundTasks" not in _read(WIDGET)
    assert "BackgroundTasks" not in _read(WIDGET_VIEWS)


# --- one bounded pass, the way the last poll went ---------------------------


def test_the_run_is_budgeted_under_ios_thirty_seconds_and_cancelled_at_expiry():
    refresh = _read(REFRESH)
    m = re.search(r"static let budgetSeconds: TimeInterval = (\d+)", refresh)
    assert m and int(m.group(1)) < 30
    handle = _body(refresh, "private static func handle(")
    assert "task.expirationHandler = { work.cancel() }" in handle
    assert "task.setTaskCompleted(success: ok)" in handle


def test_the_background_pass_never_opens_the_outbox():
    """A queued card is a write, and away it asks for Face ID. From a process
    with nobody in front of it that is a sheet on a dark phone."""
    handle = _body(_read(REFRESH), "private static func handle(")
    assert "onLive" not in handle.replace("minus `onLive`", "")
    assert "OutboxStore" not in _read(REFRESH)


def test_the_check_in_goes_the_way_the_last_poll_went():
    client = _read(CLIENT)
    assert 'static let lastKnownAwayKey = "lastKnownAway"' in client
    via = client[client.index("@Published var via: Via = .lan {"):]
    via = via[:via.index("\n    }\n") + 1]
    assert "forKey: Self.lastKnownAwayKey" in via
    body = _body(client, "func backgroundRefresh(")
    assert "UserDefaults.standard.bool(forKey: Self.lastKnownAwayKey)" in body
    away = body[body.index("if away, let channel"):body.index("let probes")]
    assert away.index("pollViaRelay") < away.index("pollDirect")
    home = body[body.index("let probes"):]
    assert "hostCandidates(record).prefix(2)" in home
    assert home.index("pollDirect") < home.index("pollViaRelay")
    # Every leg is a bounded probe: nothing in here keeps the 8s patience.
    assert "patientFirst: true" not in body
    assert body.count("patientFirst: false") == 2
    # Torn down on the way out, whatever happened.
    assert "defer { stop() }" in body


def test_the_relay_poll_takes_a_deadline_and_says_whether_it_landed():
    """One deadline for the whole check-in, and each leg reads the clock when
    it starts. A slice reserved up front is how the state leg came to be
    handed less time than the Mac's own idle wake-up (a 30-second gap once a
    channel has been quiet for ten minutes, which a fifteen-minute background
    floor always meets), so it was abandoned every run."""
    client = _read(CLIENT)
    body = _body(client, "private func pollViaRelay(")
    assert "deadline: Date? = nil) async -> Bool" in body
    assert "let stateLeg = Self.relayLeg(deadline)" in body
    assert "timeout: stateLeg)" in body
    assert "return true" in body
    # The bars are the leg dropped when the budget runs thin; the counts,
    # which are what the tile draws, have already landed.
    assert "usageLeg >= Self.usageLegFloor {" in body
    assert "return min(relayLegCap, deadline.timeIntervalSinceNow)" in client
    # The background run hands the state leg everything bar one home probe.
    run = _body(client, "func backgroundRefresh(record: PairingRecord, "
                        "budget: TimeInterval) async -> Bool {")
    assert "let deadline = Date().addingTimeInterval(budget)" in run
    assert "deadline.addingTimeInterval(-Self.directProbe))" in run
    assert "usageSlice" not in run


# --- the tile's clock follows the schedule ------------------------------------


def test_the_summary_carries_its_own_dim_horizon_and_decodes_without_it():
    summary = _read(SUMMARY)
    assert "var dimAfter: Double = 600" in summary
    assert 'decodeIfPresent(Double.self, forKey: .dimAfter) ?? 600' in summary
    assert "dimAfter: BackgroundRefresh.dimAfterSeconds" in _read(CLIENT)
    refresh = _read(REFRESH)
    assert "max(600, TimeInterval(minutes * 60) + 300)" in refresh
    widget = _read(WIDGET)
    assert "let dimAfter = summary?.dimAfter ?? 600" in widget
    assert "addingTimeInterval(dimAfter)" in widget
    assert "summary.generatedAt > summary.dimAfter" in _read(WIDGET_VIEWS)
    assert "private let staleAfter" not in _read(WIDGET_VIEWS)


def test_the_interval_is_chosen_on_the_profile_screen():
    refresh = _read(REFRESH)
    assert "static let intervals = [15, 30, 60]" in refresh
    assert "static let defaultMinutes = 15" in refresh
    profile = _read(PROFILE)
    assert "ForEach(BackgroundRefresh.intervals" in profile
    assert "BackgroundRefresh.minutes = minutes" in profile
    assert "earliest refresh" in profile
    assert (
        "The chosen interval is the earliest requested background refresh. "
        "iOS decides when updates arrive; timing is not guaranteed."
    ) in profile
    assert "refresh every" not in profile
    assert "at least this often" not in profile
    assert "lastHeardRow" in profile
    assert "last heard" in profile
    assert "request.earliestBeginDate" in refresh


# --- the Keychain is readable with the screen off ---------------------------


def test_the_pairing_is_readable_after_first_unlock_and_moved_in_place():
    pairing = _read(PAIRING)
    assert "kSecAttrAccessibleWhenUnlockedThisDeviceOnly" not in pairing
    assert pairing.count("kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly") == 3
    relax = _body(pairing, "private static func relaxAccessibility(")
    assert "SecItemUpdate(" in relax
    assert "SecItemDelete" not in relax and "SecItemAdd" not in relax
    load = _body(pairing, "func load() {")
    assert "relaxAccessibility(service: service, account: account)" in load


def test_the_background_check_in_never_fetches_the_diary():
    """One bounded state check-in; the diary (`fetchLog`) is the foreground's
    and is gated off for the whole of `backgroundRefresh`."""
    text = _read(REFRESH)
    assert "fetchLog" not in text
    assert "/api/log" not in text
    client = _read(CLIENT)
    assert "backgroundRun = true" in _body(client, "func backgroundRefresh(")
