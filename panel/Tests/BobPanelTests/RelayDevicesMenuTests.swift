import XCTest
@testable import BobPanel

/// The Devices menu's away rows, and the one rule that shapes all of them:
/// they exist only when the daemon *stated* the away path (`remote_enabled`
/// present). An older daemon publishes no such key, and absent draws
/// nothing — never an "off" toggle for a switch the daemon cannot flip.
final class RelayDevicesMenuTests: XCTestCase {

    private func flatRows(settings: DaemonClient.Settings = .init(),
                          devices: Devices,
                          awayOffArmed: String? = nil) -> [SettingsRow] {
        SettingsMenuModel.flattened(SettingsMenuModel.rows(
            settings: settings,
            context: DaemonClient.PanelContext(),
            enrollment: Enrollment(),
            devices: devices,
            recordingShortcut: false,
            unenrolArmed: nil,
            unpairArmed: nil,
            awayOffArmed: awayOffArmed))
    }

    private func statedDevices() -> Devices {
        var devices = Devices()
        devices.available = true
        devices.remoteStated = true
        return devices
    }

    func testOlderDaemonDrawsNoAwayRows() {
        var devices = Devices()
        devices.available = true
        let flat = flatRows(devices: devices)
        XCTAssertFalse(flat.contains { row in
            if case .toggle(let action, _) = row.kind {
                return action == "set_remote_access"
            }
            return false
        })
        XCTAssertFalse(flat.contains { $0.id == "custom:relayAddress" })
        XCTAssertFalse(flat.contains { $0.id.hasPrefix("info:device-lease:") })
    }

    func testStatedDaemonDrawsToggleAndRelayAddressRow() {
        var settings = DaemonClient.Settings()
        settings.remoteAccess = true
        let flat = flatRows(settings: settings, devices: statedDevices())
        let toggle = flat.first { row in
            if case .toggle(let action, _) = row.kind {
                return action == "set_remote_access"
            }
            return false
        }
        XCTAssertNotNil(toggle)
        if case .toggle(_, let isOn)? = toggle?.kind {
            XCTAssertTrue(isOn)
        }
        XCTAssertTrue(flat.contains { $0.id == "custom:relayAddress" })
    }

    /// The away window renews in exactly one place — a check-in on the Wi-Fi
    /// door — so away access with phone access off is a window that runs
    /// down and can never be renewed. The menu has to say so, or the phone's
    /// "come home to renew" sends somebody home to no effect.
    func testAwayWithoutPhoneAccessSaysTheWindowCannotRenew() {
        var settings = DaemonClient.Settings()
        settings.remoteAccess = true
        var devices = statedDevices()
        devices.lanEnabled = false
        XCTAssertTrue(flatRows(settings: settings, devices: devices)
            .contains { $0.id == "info:away-needs-lan" })

        devices.lanEnabled = true
        XCTAssertFalse(flatRows(settings: settings, devices: devices)
            .contains { $0.id == "info:away-needs-lan" })
    }

    /// A keyless device means two different things either side of the
    /// switch, and only one of them has a way out.
    func testKeylessDeviceNamesRePairingOnlyWhileAwayIsOn() {
        let device = PairedDevice(id: "abc", name: "Kitchen")
        XCTAssertEqual(SettingsMenuModel.leaseLine(device, remoteEnabled: true),
                       "no away access — re-pair at home to add it")
        XCTAssertEqual(SettingsMenuModel.leaseLine(device, remoteEnabled: false),
                       "no away access")
    }

    // MARK: - The home line

    /// A keyless device on a daemon that takes sealed home frames is one
    /// paired before the change: it must re-pair, and the menu says so.
    func testKeylessDeviceOnANewDaemonSaysPairItAgain() {
        let device = PairedDevice(id: "abc", name: "Kitchen")
        XCTAssertEqual(SettingsMenuModel.homeLine(device, homeSealed: true),
                       "paired before sealed home access — pair it again")
    }

    func testKeyedDeviceDrawsNoHomeLine() {
        var device = PairedDevice(id: "abc", name: "Kitchen")
        device.home = true
        XCTAssertNil(SettingsMenuModel.homeLine(device, homeSealed: true))
    }

    /// An older daemon publishes no `home` on any device and no
    /// `home_sealed`: absent is not "re-pair everything".
    func testOlderDaemonDrawsNoHomeLine() {
        let device = PairedDevice(id: "abc", name: "Kitchen")
        XCTAssertNil(SettingsMenuModel.homeLine(device, homeSealed: false))
    }

    func testTheHomeLineIsDrawnUnderTheDeviceIdRegardlessOfAway() {
        var devices = Devices()
        devices.available = true
        devices.homeSealed = true
        devices.devices = [PairedDevice(id: "abc", name: "Kitchen")]
        // No `remoteStated`: the away rows are absent, the home line is not.
        let flat = flatRows(devices: devices)
        XCTAssertTrue(flat.contains { $0.id == "info:device-home:abc" })
        XCTAssertFalse(flat.contains { $0.id == "info:device-lease:abc" })
        var keyed = PairedDevice(id: "abc", name: "Kitchen")
        keyed.home = true
        devices.devices = [keyed]
        XCTAssertFalse(flatRows(devices: devices)
            .contains { $0.id == "info:device-home:abc" })
    }

    func testHomeFieldsDecodeTolerantly() throws {
        let snap = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "devices": {"available": true, "home_sealed": true,
                     "devices": [{"id": "abc", "home": true},
                                 {"id": "old"}]}}
        """.utf8))
        XCTAssertTrue(snap.devices.homeSealed)
        XCTAssertEqual(snap.devices.devices.first?.home, true)
        XCTAssertEqual(snap.devices.devices.last?.home, false)
        let old = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "devices": {"available": true, "devices": [{"id": "xyz"}]}}
        """.utf8))
        XCTAssertFalse(old.devices.homeSealed)
        XCTAssertEqual(old.devices.devices.first?.home, false)
    }

    func testPerDeviceLeaseLineStates() {
        var device = PairedDevice(id: "abc", name: "Kitchen")
        XCTAssertEqual(SettingsMenuModel.leaseLine(device), "no away access")
        device.relay = true
        device.leaseExpiresAt = Date().timeIntervalSince1970 - 60
        XCTAssertEqual(SettingsMenuModel.leaseLine(device),
                       "away access lapsed — reads continue")
        device.leaseExpiresAt = Date().timeIntervalSince1970 + 3600
        XCTAssertTrue(SettingsMenuModel.leaseLine(device)
            .hasPrefix("away access until "))
    }

    func testDoneRemotelySubmenuOnlyWithActivity() {
        var devices = statedDevices()
        XCTAssertFalse(flatRows(devices: devices)
            .contains { $0.id == "submenu:remote-activity" })
        let entry = try? JSONDecoder().decode(RemoteAction.self, from: Data(
            #"{"device_id": "abc", "action": "stop_session", "at": 1, "ok": true}"#
                .utf8))
        devices.remoteActivity = [entry].compactMap { $0 }
        XCTAssertEqual(devices.remoteActivity.count, 1)
        XCTAssertTrue(flatRows(devices: devices)
            .contains { $0.id == "submenu:remote-activity" })
    }

    func testNewDevicesFieldsDecodeTolerantly() throws {
        let snap = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "devices": {"available": true, "lan_enabled": true, "port": 19875,
                     "remote_enabled": true, "relay_url": "https://r.example",
                     "remote_activity": [{"device_id": "abc",
                                          "action": "dismiss",
                                          "at": 5, "ok": false}],
                     "devices": [{"id": "abc", "name": "Kitchen",
                                  "relay": true,
                                  "lease_expires_at": 99.5}]}}
        """.utf8))
        XCTAssertTrue(snap.devices.remoteStated)
        XCTAssertTrue(snap.devices.remoteEnabled)
        XCTAssertEqual(snap.devices.relayURL, "https://r.example")
        XCTAssertEqual(snap.devices.remoteActivity.count, 1)
        XCTAssertEqual(snap.devices.remoteActivity.first?.action, "dismiss")
        XCTAssertEqual(snap.devices.remoteActivity.first?.ok, false)
        XCTAssertEqual(snap.devices.devices.first?.relay, true)
        XCTAssertEqual(snap.devices.devices.first?.leaseExpiresAt ?? 0,
                       99.5, accuracy: 0.001)
        // The old rows keep decoding without the new keys.
        let old = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "devices": {"available": true, "devices": [{"id": "xyz"}]}}
        """.utf8))
        XCTAssertFalse(old.devices.remoteStated)
        XCTAssertEqual(old.devices.devices.first?.relay, false)
        XCTAssertEqual(old.devices.devices.first?.leaseExpiresAt, 0)
    }

    // MARK: - The away link's health line

    private func device(id: String = "phone",
                        lastFrameAt: Double = 0) -> PairedDevice {
        var device = PairedDevice(id: id, name: "Kitchen")
        device.relay = true
        device.leaseExpiresAt = Date().timeIntervalSince1970 + 3600
        device.lastFrameAt = lastFrameAt
        return device
    }

    private func health(_ json: String) throws -> RelayHealth {
        try JSONDecoder().decode(RelayHealth.self, from: Data(json.utf8))
    }

    func testHealthLineIsAbsentOnAnOlderDaemon() {
        // `stated` false: the daemon said nothing, and nothing is drawn.
        XCTAssertNil(SettingsMenuModel.awayHealthLine(
            device(), health: RelayHealth()))
    }

    func testHealthLineSaysOkAndWhenItLastCarried() throws {
        let now = Date()
        let line = SettingsMenuModel.awayHealthLine(
            device(lastFrameAt: now.timeIntervalSince1970 - 600),
            health: try health(#"{"state": "ok", "last_ok_at": 1700}"#),
            now: now)
        XCTAssertEqual(line, "away link OK — last carried a message 10m ago")
    }

    func testHealthLineSaysOkWithNothingCarriedYet() throws {
        let line = SettingsMenuModel.awayHealthLine(
            device(lastFrameAt: 0),
            health: try health(#"{"state": "ok", "last_ok_at": 1700}"#))
        XCTAssertEqual(line, "away link OK — nothing carried yet")
    }

    func testHealthLineNamesTheFailureAndItsAge() throws {
        let line = SettingsMenuModel.awayHealthLine(
            device(lastFrameAt: 1),
            health: try health(
                #"{"state": "failing", "status": 429, "failing_for": 3600}"#))
        XCTAssertEqual(line, "away link failing (status 429) for 60m")
    }

    func testAFailureWithNoAnswerAtAllSaysSo() throws {
        let line = SettingsMenuModel.awayHealthLine(
            device(),
            health: try health(
                #"{"state": "failing", "status": 0, "failing_for": 30}"#))
        XCTAssertEqual(line, "away link failing (no answer) for 30s")
    }

    func testTheHealthRowIsDrawnBesideTheLeaseRow() {
        var devices = statedDevices()
        devices.devices = [device(lastFrameAt: Date().timeIntervalSince1970)]
        var snapshotHealth = RelayHealth()
        snapshotHealth.stated = true
        snapshotHealth.state = "ok"
        devices.relayHealth = snapshotHealth
        let flat = flatRows(devices: devices)
        XCTAssertTrue(flat.contains { $0.id == "info:device-lease:phone" })
        XCTAssertTrue(flat.contains { $0.id == "info:device-away:phone" })
    }

    func testTheHealthRowIsAbsentWhenTheDaemonStatedNoHealth() {
        var devices = statedDevices()
        devices.devices = [device(lastFrameAt: Date().timeIntervalSince1970)]
        let flat = flatRows(devices: devices)
        XCTAssertTrue(flat.contains { $0.id == "info:device-lease:phone" })
        XCTAssertFalse(flat.contains { $0.id == "info:device-away:phone" })
    }

    // MARK: - The away grant

    /// A stated daemon with one keyed phone carrying a grant.
    private func grantedDevices(_ days: Int,
                                relay: Bool = true) -> Devices {
        var devices = statedDevices()
        var device = PairedDevice(id: "abc", name: "Kitchen")
        device.relay = relay
        device.leaseDays = days
        device.leaseExpiresAt = Date().timeIntervalSince1970 + 3600
        devices.devices = [device]
        return devices
    }

    func testTheLeaseLineNamesTheGrantAndTheTime() {
        var device = PairedDevice(id: "abc", name: "Kitchen")
        device.relay = true
        device.leaseDays = 7
        let now = Date()
        device.leaseExpiresAt = now.timeIntervalSince1970 + 3600
        let line = SettingsMenuModel.leaseLine(device, now: now)
        XCTAssertTrue(line.hasPrefix("7 days granted — away access until "))
    }

    /// A grant may now be a fortnight out, so a bare time of day would name
    /// the wrong day. Today keeps today's shape exactly.
    func testTheLeaseLineGainsTheDateOnceItIsNotToday() {
        // Local noon, so "an hour from now" is unambiguously still today
        // whatever time zone the test runs in.
        let now = Calendar.current.date(
            bySettingHour: 12, minute: 0, second: 0, of: Date())!
        let today = SettingsMenuModel.expiryWords(
            now.addingTimeInterval(3600), now: now)
        let later = SettingsMenuModel.expiryWords(
            now.addingTimeInterval(3 * 24 * 3600), now: now)
        XCTAssertNotEqual(today, later)
        XCTAssertTrue(later.count > today.count)
    }

    func testTheGrantRowsAreAbsentOnAnOlderDaemon() {
        // `remote_enabled` absent: the whole away block is absent.
        var devices = grantedDevices(7)
        devices.remoteStated = false
        XCTAssertFalse(flatRows(devices: devices)
            .contains { $0.id.hasPrefix("custom:away-days:") })
        // Stated away, but no grant published: -1 draws nothing.
        XCTAssertFalse(flatRows(devices: grantedDevices(-1))
            .contains { $0.id.hasPrefix("custom:away-days:") })
        XCTAssertFalse(flatRows(devices: grantedDevices(-1))
            .contains { $0.id.hasPrefix("info:device-grant:") })
    }

    func testTheFourGrantRowsAreDrawnForAKeyedPhone() {
        let flat = flatRows(devices: grantedDevices(7))
        let ids = flat.map(\.id).filter { $0.hasPrefix("custom:away-days:") }
        XCTAssertEqual(ids, ["custom:away-days:abc:1",
                             "custom:away-days:abc:3",
                             "custom:away-days:abc:7",
                             "custom:away-days:abc:14"])
        let first = flat.first { $0.id == "custom:away-days:abc:1" }
        XCTAssertEqual(first?.title, "Grant 24 hours")
        XCTAssertEqual(flat.first { $0.id == "custom:away-days:abc:14" }?.title,
                       "Grant 14 days")
        if case .custom(let action)? = first?.kind {
            XCTAssertEqual(action, .awayDays(deviceId: "abc", days: 1))
        } else {
            XCTFail("the grant row is not a custom action")
        }
        // A keyless phone has no away channel to grant anything on.
        XCTAssertFalse(flatRows(devices: grantedDevices(7, relay: false))
            .contains { $0.id.hasPrefix("custom:away-days:") })
    }

    func testTheGrantInfoLineSaysWhichLengthIsInForce() {
        let flat = flatRows(devices: grantedDevices(3))
        XCTAssertEqual(flat.first { $0.id == "info:device-grant:abc" }?.title,
                       "one check-in at home buys 3 days")
        let off = flatRows(devices: grantedDevices(0))
        XCTAssertEqual(off.first { $0.id == "info:device-grant:abc" }?.title,
                       "away access is off for this phone")
    }

    /// Zero is "somebody ended it", and there is nothing left to end.
    func testTheEndRowIsAbsentOnceAwayAccessIsOff() {
        XCTAssertFalse(flatRows(devices: grantedDevices(0))
            .contains { $0.id == "custom:away-off:abc" })
        XCTAssertTrue(flatRows(devices: grantedDevices(1))
            .contains { $0.id == "custom:away-off:abc" })
    }

    func testTheEndRowArmsBeforeItFires() {
        let plain = flatRows(devices: grantedDevices(7))
            .first { $0.id == "custom:away-off:abc" }
        XCTAssertEqual(plain?.title, "End away access now")
        let armed = flatRows(devices: grantedDevices(7), awayOffArmed: "abc")
            .first { $0.id == "custom:away-off:abc" }
        XCTAssertEqual(armed?.title, "End away access now — really?")
        // Arming one phone leaves another's row alone.
        var two = grantedDevices(7)
        var other = PairedDevice(id: "xyz", name: "Studio")
        other.relay = true
        other.leaseDays = 7
        other.leaseExpiresAt = Date().timeIntervalSince1970 + 3600
        two.devices.append(other)
        XCTAssertEqual(flatRows(devices: two, awayOffArmed: "abc")
            .first { $0.id == "custom:away-off:xyz" }?.title,
            "End away access now")
    }

    func testTheGrantDecodesTolerantly() throws {
        let snap = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "devices": {"available": true, "remote_enabled": true,
                     "devices": [{"id": "abc", "lease_days": 14},
                                 {"id": "off", "lease_days": 0},
                                 {"id": "old"}]}}
        """.utf8))
        XCTAssertEqual(snap.devices.devices.first?.leaseDays, 14)
        XCTAssertEqual(snap.devices.devices[1].leaseDays, 0)
        // Absent must never read as 0 — that is "away access ended".
        XCTAssertEqual(snap.devices.devices.last?.leaseDays, -1)
    }

    // MARK: - The socket lane

    private func toggle(_ rows: [SettingsRow], _ action: String) -> SettingsRow? {
        rows.first { row in
            if case .toggle(let name, _) = row.kind { return name == action }
            return false
        }
    }

    /// An older daemon publishes no `relay_ws_enabled`: no toggle, no
    /// address row, no per-device line — even with away access stated.
    func testRelayWSAbsentDrawsNoSocketRows() {
        var devices = statedDevices()
        devices.devices = [device(lastFrameAt: 1)]
        let flat = flatRows(devices: devices)
        XCTAssertNil(toggle(flat, "set_relay_ws"))
        XCTAssertFalse(flat.contains { $0.id == "custom:relaySocketAddress" })
        XCTAssertFalse(flat.contains { $0.id.hasPrefix("info:device-socket:") })
        // Stated on the daemon but away access not stated: nothing either,
        // because the socket rows sit under the away block.
        var awayless = Devices()
        awayless.available = true
        awayless.relayWSStated = true
        XCTAssertNil(toggle(flatRows(devices: awayless), "set_relay_ws"))
    }

    func testRelayWSStatedDrawsTheToggleTheAddressRowAndTheDeviceLine() {
        var settings = DaemonClient.Settings()
        settings.remoteAccess = true
        settings.relayWS = true
        var devices = statedDevices()
        devices.relayWSStated = true
        var phone = device(lastFrameAt: 1)
        phone.socket = "armed"
        devices.devices = [phone]
        let flat = flatRows(settings: settings, devices: devices)
        let row = toggle(flat, "set_relay_ws")
        XCTAssertNotNil(row)
        XCTAssertEqual(row?.title, "Socket link")
        if case .toggle(_, let isOn)? = row?.kind { XCTAssertTrue(isOn) }
        XCTAssertTrue(flat.contains { $0.id == "custom:relaySocketAddress" })
        if case .custom(let action)? = flat.first(where: { $0.id == "custom:relaySocketAddress" })?.kind {
            XCTAssertEqual(action, .relayAddress)
        } else {
            XCTFail("the socket address row does not open the relay sheet")
        }
        XCTAssertEqual(flat.first { $0.id == "info:device-socket:phone" }?.title,
                       "socket armed")
        // The toggle follows the preference, not the daemon's flag.
        settings.relayWS = false
        if case .toggle(_, let isOn)? = toggle(
            flatRows(settings: settings, devices: devices), "set_relay_ws")?.kind {
            XCTAssertFalse(isOn)
        }
    }

    func testTheSocketLineWordsFollowTheDaemonsWord() throws {
        var phone = device()
        XCTAssertNil(SettingsMenuModel.socketLine(phone, health: RelayHealth()))
        phone.socket = "connecting"
        XCTAssertEqual(SettingsMenuModel.socketLine(phone, health: RelayHealth()),
                       "socket connecting")
        phone.socket = "open"
        XCTAssertEqual(SettingsMenuModel.socketLine(phone, health: RelayHealth()),
                       "socket open — waiting for the phone")
        phone.socket = "armed"
        XCTAssertEqual(SettingsMenuModel.socketLine(phone, health: RelayHealth()),
                       "socket armed")
        phone.socket = "off"
        XCTAssertNil(SettingsMenuModel.socketLine(phone, health: RelayHealth()))
        XCTAssertEqual(SettingsMenuModel.socketLine(
            phone, health: try health(#"{"state": "ok", "last_ok_at": 1700}"#)),
            "socket off")
        phone.socket = "stranger"
        XCTAssertNil(SettingsMenuModel.socketLine(phone, health: RelayHealth()))
    }

    /// Socket link on with no socket address stored is a standing of its
    /// own (`relay_ws.HEALTH_NO_ADDRESS`), drawn as its own sentence rather
    /// than as a failure or as nothing.
    func testNoSocketAddressDrawsItsOwnSentence() throws {
        var phone = device()
        let unset = try health(
            #"{"state": "no-address", "status": 0, "failures": 0, "failing_for": 30}"#)
        XCTAssertFalse(unset.isFailing)
        XCTAssertEqual(SettingsMenuModel.socketLine(phone, health: unset),
                       "socket link: no socket address set")
        phone.socket = "off"
        XCTAssertEqual(SettingsMenuModel.socketLine(phone, health: unset),
                       "socket link: no socket address set")
        XCTAssertEqual(SettingsMenuModel.socketLine(
            phone, health: try health(#"{"state": "bad-address", "status": 0}"#)),
            "socket link: the socket address must start with wss://")
        var devices = statedDevices()
        devices.relayWSStated = true
        devices.devices = [phone]
        devices.relayWSHealth = unset
        XCTAssertEqual(flatRows(devices: devices)
            .first { $0.id == "info:device-socket:phone" }?.title,
            "socket link: no socket address set")
    }

    func testAFailingSocketLaneDrawsTheFailingLine() throws {
        var phone = device()
        phone.socket = "connecting"
        XCTAssertEqual(SettingsMenuModel.socketLine(
            phone, health: try health(
                #"{"state": "failing", "status": 503, "failing_for": 3600}"#)),
            "socket link failing (status 503) for 60m")
        XCTAssertEqual(SettingsMenuModel.socketLine(
            phone, health: try health(
                #"{"state": "failing", "status": 0, "failing_for": 30}"#)),
            "socket link failing (no answer) for 30s")
        var devices = statedDevices()
        devices.relayWSStated = true
        devices.devices = [phone]
        var failing = RelayHealth()
        failing.stated = true
        failing.state = "failing"
        failing.status = 4001
        failing.failingFor = 120
        devices.relayWSHealth = failing
        XCTAssertEqual(flatRows(devices: devices)
            .first { $0.id == "info:device-socket:phone" }?.title,
            "socket link failing (status 4001) for 2m")
    }

    func testTheSocketFieldsDecodeTolerantly() throws {
        let snap = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "devices": {"available": true, "remote_enabled": true,
                     "relay_ws_enabled": true,
                     "relay_ws_url": "wss://sock.example",
                     "relay_ws_health": {"state": "ok", "status": 0,
                                         "failures": 0, "failing_for": 0,
                                         "last_ok_at": 1700},
                     "devices": [{"id": "abc", "socket": "armed"},
                                 {"id": "old"}]}}
        """.utf8))
        XCTAssertTrue(snap.devices.relayWSStated)
        XCTAssertTrue(snap.devices.relayWSEnabled)
        XCTAssertEqual(snap.devices.relayWSURL, "wss://sock.example")
        XCTAssertTrue(snap.devices.relayWSHealth.stated)
        XCTAssertEqual(snap.devices.relayWSHealth.state, "ok")
        XCTAssertEqual(snap.devices.devices.first?.socket, "armed")
        XCTAssertEqual(snap.devices.devices.last?.socket, "")
        let old = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at": 1, "agents": {}, "counts": {},
         "devices": {"available": true, "remote_enabled": true,
                     "devices": [{"id": "xyz"}]}}
        """.utf8))
        XCTAssertFalse(old.devices.relayWSStated)
        XCTAssertFalse(old.devices.relayWSEnabled)
        XCTAssertEqual(old.devices.relayWSURL, "")
        XCTAssertFalse(old.devices.relayWSHealth.stated)
        XCTAssertEqual(old.devices.devices.first?.socket, "")
    }

    /// The menu never claims a length for a daemon that published none.
    func testNoHardCodedDayCountOutsideTheRowTitles() {
        let flat = flatRows(devices: grantedDevices(-1))
        for row in flat where row.id == "info:away-needs-lan" {
            XCTAssertFalse(row.title.contains("24"))
        }
    }
}
