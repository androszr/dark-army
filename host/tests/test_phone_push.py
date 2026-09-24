# host/tests/test_phone_push.py
"""The phone's push path, pinned from the Mac's side.

Source pins over the Swift files (`test_phone_remote.py`'s pattern — `ios/`
has no test target by explicit decision). What is pinned is the contract:
the action name both ends spell, permission asked only once somebody exists
to buzz, a suppressed foreground banner, the unregister on forget, the badge
clear, and the deep-link scheme.
"""

from __future__ import annotations

from pathlib import Path

from dark_army_daemon.api_server import ApiServer

ROOT = Path(__file__).resolve().parents[2]

PHONE = ROOT / "ios" / "BobPhone"
PUSH = PHONE / "Push.swift"
CLIENT = PHONE / "Client.swift"
ROUTER = PHONE / "Router.swift"
APP = PHONE / "BobPhoneApp.swift"
ACTIONS = PHONE / "Actions.swift"
INFO = PHONE / "Info.plist"
ENTITLEMENTS = PHONE / "BobPhone.entitlements"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


# --- the delegate and its adaptor ----------------------------------------------


def test_the_delegate_exists_and_is_seated():
    text = _read(PUSH)
    assert "class PushDelegate" in text
    assert "UIApplicationDelegate" in text
    assert "UNUserNotificationCenterDelegate" in text
    assert "didRegisterForRemoteNotificationsWithDeviceToken" in text
    # And the app actually seats it — a delegate class nobody adapts is dead.
    assert "@UIApplicationDelegateAdaptor(PushDelegate.self)" in _read(APP)


def test_authorization_is_asked_only_when_paired():
    """A permission dialog over the QR scanner would be Dark Army asking for
    something it cannot yet use — the guard sits above the request."""
    text = _read(PUSH)
    ensure = text.split("func ensure(")[1].split("\n    }")[0]
    guard_at = ensure.index("guard record != nil else { return }")
    ask_at = ensure.index("requestAuthorization")
    assert guard_at < ask_at
    # All three call sites hand the pairing verdict in: `.active`, the
    # pairing landing, and the unlock (see the dedicated test below).
    app = _read(APP)
    assert app.count("PushRegistrar.shared.ensure(") == 3


def test_the_unlock_is_a_registration_nudge_with_a_transport_behind_it():
    """`.active` fires before the face check, when `client.stop()` has
    nilled the record — an `ensure` there has no transport, so a phone
    that only warm-returns (never cold-launches) never re-registered.
    The unlock handler, right after `client.start(record:)`, is the one
    moment both the pairing and the transport exist."""
    app = _read(APP)
    unlocked = app.split(".onChange(of: lock.unlocked)")[1].split(
        ".onChange(of: pairing.record)")[0]
    start_at = unlocked.index("client.start(record: record)")
    ensure_at = unlocked.index("PushRegistrar.shared.ensure(record: record)")
    assert start_at < ensure_at


def test_the_permission_alert_does_not_lock_the_app():
    """The system's notification-permission alert drives the scene
    `.inactive` exactly as a dictation consent alert does; locking there
    demanded a second Face ID right after Allow. The registrar raises
    `requestingConsent` *before* the ask, and the app ORs it into the
    same carve-out dictation already has."""
    push = _read(PUSH)
    ensure = push.split("func ensure(")[1].split("\n    }")[0]
    raised_at = ensure.index("requestingConsent = true")
    ask_at = ensure.index("requestAuthorization")
    assert raised_at < ask_at
    # And lowered once the alert is answered, whatever the answer was.
    assert "PushRegistrar.shared.requestingConsent = false" in ensure
    app = _read(APP)
    inactive = app.split("case .inactive:")[1].split("case .background:")[0]
    assert "PushRegistrar.shared.requestingConsent" in inactive
    assert "DictationEngine.shared.requestingConsent" in inactive


def test_foreground_presentation_is_suppressed():
    """The screen already shows the state; a banner over it restates less."""
    text = _read(PUSH)
    will_present = text.split("willPresent")[1].split("\n    }")[0]
    assert "return []" in will_present


def test_the_action_name_matches_the_mac_end():
    """One string, both ends. The Swift constant must spell exactly the name
    `ApiServer` chose, and that name must be a chosen LAN action."""
    assert 'static let registerPushToken = "register_push_token"' \
        in _read(ACTIONS)
    assert "register_push_token" in ApiServer.LAN_ACTIONS
    assert "register_push_token" in ApiServer.REMOTE_ACTIONS


def test_a_tap_routes_through_the_router_never_past_the_lock():
    """The tap parks a tab in `PhoneRouter`'s slot; `ContentView` — which
    only exists behind the Face-ID gate — consumes it. The delegate must
    never navigate directly."""
    push = _read(PUSH)
    did_receive = push.split("didReceive")[1].split("\n    }")[0]
    assert "PhoneRouter.shared.go(.needs)" in did_receive
    router = _read(ROUTER)
    assert "func take()" in router
    app = _read(APP)
    assert "router.take()" in app


def test_unregister_on_forget_before_the_record_dies():
    """Forget captures the record *and* the away channel
    (`client.pushTeardown()`) before `stop()` tears them down, then sends
    the empty-token unregister on them — best-effort, behind the teardown,
    never a Face ID prompt mid-forget; the Mac-side un-pair stays the
    authoritative kill."""
    app = _read(APP)
    forget = app.split("private func forget()")[1].split("\n    }\n}")[0]
    capture_at = forget.index("client.pushTeardown()")
    stop_at = forget.index("client.stop()")
    assert capture_at < stop_at
    assert "PushRegistrar.shared.unregister(" in forget
    push = _read(PUSH)
    unregister = push.split("func unregister(")[1].split("\n    }")[0]
    assert '"token": ""' in unregister
    # The captured channel keeps its own counters, so the last sealed send
    # never replays a number the live channel already spent.
    client = _read(CLIENT)
    teardown = client.split("func pushTeardown()")[1].split("\n    }")[0]
    assert "return (record, channel, homeChannel)" in teardown


def test_the_badge_clears_on_foreground():
    assert "setBadgeCount(0)" in _read(PUSH)
    app = _read(APP)
    active = app.split("case .active:")[1].split("case .inactive:")[0]
    assert "clearBadge()" in active
    # The 24h re-registration nudge rides the same moment.
    assert "PushRegistrar.shared.ensure(" in active


def test_delivered_banners_clear_with_the_badge_and_on_a_quiet_mac():
    """A buzz is the Mac's waiting count at the moment it was composed. Once
    the person has the list on screen, or the Mac itself reports that nobody
    needs them, a banner still in Notification Center is a stale alert about
    a row the desk has dealt with — the badge already went by that rule."""
    push = _read(PUSH)
    badge = push.split("func clearBadge()")[1].split("\n    }")[0]
    assert "removeAllDeliveredNotifications()" in badge
    quiet = push.split("func clearDeliveredIfQuiet(needsYou: Int)")[1] \
        .split("\n    }")[0]
    assert "guard needsYou == 0 else { return }" in quiet
    assert "removeAllDeliveredNotifications()" in quiet
    client = _read(CLIENT)
    applied = client.split("private func applyState(")[1].split("return .applied")[0]
    assert "clearDeliveredIfQuiet(" in applied
    # Only a foreground apply: a background refresh has no screen to be the
    # truth, and its banners are exactly what it was woken to leave.
    assert "if !backgroundRun {" in applied


def test_registration_heals_a_rotated_token():
    """iOS rotates tokens whenever it likes; the registrar re-sends when the
    token changed and at least daily, and a refusal keeps the stamp old so
    the next foreground retries."""
    text = _read(PUSH)
    assert "resendInterval: TimeInterval = 24 * 60 * 60" in text
    assert "currentToken != sent || aged" in text


def test_every_registrar_send_rides_the_quiet_route():
    """Both sends — the registration and the unregister — ride
    `PhoneClient.quietPost`: `post`'s exact walk (LAN hosts first, then the
    sealed relay rung), minus the RemoteAuth prompt. The relay rung is the
    point — a phone that reaches its Mac only through the mailbox never
    registered at all when the walk was LAN-only; and the missing prompt is
    safe because the verb only names where this phone's own buzzes go, with
    the Mac still checking the device, the lease and REMOTE_ACTIONS."""
    push = _read(PUSH)
    # The registrar owns no transport of its own any more.
    assert "URLSession" not in push
    assert "client.registerPush(" in push
    assert "client.quietPost(" in push
    # And never `post`, whose relay rung raises "Confirm it's you".
    assert ".post(action:" not in push
    assert "RemoteAuth.shared" not in push
    client = _read(CLIENT)
    quiet = client.split("func quietPost(")[1].split("\n    }")[0]
    assert 'channel.request(kind: "action"' in quiet
    # The comment may *name* the prompt it skips; nothing may call it.
    assert "RemoteAuth.shared" not in quiet
    register = client.split("func registerPush(")[1].split("\n    }")[0]
    assert "PhoneActions.registerPushToken" in register


def test_the_env_names_the_apns_host_for_this_build():
    text = _read(PUSH)
    assert '#if DEBUG' in text
    assert 'return "dev"' in text
    assert 'return "prod"' in text


# --- the plumbing around the code ----------------------------------------------


def test_the_scheme_is_registered():
    info = _read(INFO)
    assert "CFBundleURLSchemes" in info
    assert "<string>bobphone</string>" in info


def test_the_app_entitlements_carry_push():
    text = _read(ENTITLEMENTS)
    assert "aps-environment" in text
    # `development` in the file; automatic signing flips it to production
    # at app-store export.
    assert "<string>development</string>" in text


def test_testflight_signs_with_the_communication_capability_and_falls_back():
    """23 Sep 2026: without the capability `updating(from:)` throws and a
    buzz shows the app icon with a thumbnail, never the agent's face. The
    TestFlight archive is signed with the `.communication` siblings; an
    export the App ID refuses re-signs with the plain files and ships with
    a warning. The plain files stay capability-free for that fallback."""
    key = "<key>com.apple.developer.usernotifications.communication</key>"
    signed_app = _read(ENTITLEMENTS)
    signed_ext = _read(ROOT / "ios" / "BobPhoneNotification"
                       / "BobPhoneNotification.entitlements")
    assert "aps-environment" in signed_app
    assert key not in signed_app
    assert key not in signed_ext
    comm_app = _read(ROOT / "ios" / "BobPhone" / "BobPhone.communication.entitlements")
    assert comm_app.count(key) == 1 and "aps-environment" in comm_app
    assert "group.$(DARK_ARMY_BUNDLE_ID)" in comm_app
    comm_ext = _read(ROOT / "ios" / "BobPhoneNotification"
                     / "BobPhoneNotification.communication.entitlements")
    assert comm_ext.count(key) == 1
    assert "application-groups" not in comm_ext
    yml = _read(ROOT / ".github" / "workflows" / "testflight.yml")
    embed = yml.split("- name: Embed the entitlements", 1)[1].split("- name:", 1)[0]
    assert "BobPhone/BobPhone.communication.entitlements" in embed
    assert "BobPhoneNotification.communication.entitlements" in embed
    export = yml.split("- name: Export a distribution build", 1)[1].split("- name:", 1)[0]
    assert "if ! export_build; then" in export
    assert "BobPhoneNotification/BobPhoneNotification.entitlements" in export
    assert "BobPhone/BobPhone.entitlements" in export
    assert "::error::The notification appex carries the communication" not in yml
    info = _read(ROOT / "ios" / "BobPhone" / "Info.plist")
    assert "<key>NSUserActivityTypes</key>" in info
    assert "<string>INSendMessageIntent</string>" in info


def test_the_entitlements_are_wired_into_both_app_configurations():
    pbx = _read(PBXPROJ)
    assert pbx.count(
        "CODE_SIGN_ENTITLEMENTS = BobPhone/BobPhone.entitlements;") == 2


def test_the_deep_link_is_applied_post_unlock():
    """`onOpenURL` writes the router's slot; nothing in the App scene
    navigates directly on it."""
    app = _read(APP)
    assert "PhoneRouter.shared.open(url)" in app
    router = _read(ROUTER)
    # The scheme is `FleetLinks.scheme` ("bobphone"), the one builder the
    # widget's face links and this reader share.
    assert "url.scheme == FleetLinks.scheme" in router
    assert 'static let scheme = "bobphone"' in _read(ROOT / "ios" / "Shared" / "FleetSummary.swift")
