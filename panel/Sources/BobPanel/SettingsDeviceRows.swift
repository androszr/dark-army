import Foundation

// The Devices group: the two doors, one submenu per paired phone and the
// remote-activity log. Split out of `SettingsMenuModel.swift` on 20 Sep 2026.
extension SettingsMenuModel {
    static func deviceRows(_ devices: Devices,
                                   lanAccess: Bool,
                                   remoteAccess: Bool,
                                   relayWS: Bool = false,
                                   unpairArmed: String?,
                                   awayOffArmed: String? = nil) -> [SettingsRow] {
        var rows: [SettingsRow] = [
            toggle(
                "Phone access",
                action: "set_lan_access", isOn: lanAccess,
                tooltip: "Let a paired iPhone on this Wi-Fi read the fleet and "
                    + "the board, and act: file a card, start or stop work, "
                    + "reply, and close a leftover terminal. Off, nothing on "
                    + "the network can reach Dark Army."),
        ]
        // What exposure remains while the door is open, said under the
        // switch and only while the listener is actually bound
        // (`lanEnabled`, not the preference): the seal is the boundary on
        // a shared network, and the person should know the door is seen.
        if devices.lanEnabled {
            rows.append(info(
                "While Phone access is on, every device on this Wi-Fi can "
                    + "see the door — a hotel or café network too. Only a "
                    + "paired phone's sealed requests get an answer; anything "
                    + "arriving over a VPN is turned away.",
                id: "info:lan-exposure"))
        }
        // The away path's rows exist only when the daemon *said* anything
        // about it (`remoteStated`) — an older daemon publishes no
        // `remote_enabled` key, and absent must draw nothing, never "off".
        if devices.remoteStated {
            rows.append(toggle(
                "Away access",
                action: "set_remote_access", isOn: remoteAccess,
                tooltip: "Let the paired phone keep watching and acting for "
                    + "as long as you grant it after it last checked in on "
                    + "this Wi-Fi, through the sealed relay you deploy "
                    + "yourself. Off, the Mac never contacts the relay at "
                    + "all."))
            rows.append(SettingsRow(
                id: "custom:relayAddress",
                title: "Relay address…",
                kind: .custom(.relayAddress)))
            // The socket lane's rows exist only when the daemon *said*
            // anything about it (`relayWSStated`) — an older daemon
            // publishes no `relay_ws_enabled`, and absent draws nothing.
            if devices.relayWSStated {
                rows.append(toggle(
                    "Socket link",
                    action: "set_relay_ws", isOn: relayWS,
                    tooltip: "A trial: hold one live, sealed line per paired "
                        + "phone to the socket relay you deploy yourself, so "
                        + "the picture reaches the phone the moment it changes "
                        + "and a press lands in under a second. Off by "
                        + "default; needs Away access on and a socket address; "
                        + "the mailbox stays the fallback either way."))
                rows.append(SettingsRow(
                    id: "custom:relaySocketAddress",
                    title: "Socket address…",
                    kind: .custom(.relayAddress)))
            }
            // The away window is renewed in exactly one place — a phone
            // check-in arriving on the Wi-Fi door — so with Phone access off
            // it runs down and can never be renewed. Saying nothing left the
            // phone telling somebody to come home and renew, and coming home
            // doing nothing. The two switches stay independent (turning one
            // off must never silently turn the other off); the dependency is
            // stated instead.
            if !devices.lanEnabled {
                rows.append(info(
                    "Away access needs Phone access on — the away window "
                        + "only renews on this Wi-Fi",
                    id: "info:away-needs-lan"))
            }
        }
        if !devices.available {
            rows.append(info("This daemon does not report paired devices",
                             id: "info:devices"))
        } else if devices.devices.isEmpty {
            rows.append(info("No device is paired", id: "info:devices"))
        } else {
            for device in devices.devices {
                let armed = unpairArmed == device.id
                var deviceEntries = [
                    info(device.id, id: "info:device-id:\(device.id)"),
                ]
                // Independent of `remoteStated`: the home door is a
                // different door from the away one.
                if let line = homeLine(device, homeSealed: devices.homeSealed) {
                    deviceEntries.append(info(
                        line, id: "info:device-home:\(device.id)"))
                }
                if devices.remoteStated {
                    // The bot is not on the day window: its reads and
                    // writes are the two grants drawn below instead.
                    if device.botAccess == nil {
                        deviceEntries.append(info(
                            leaseLine(device, remoteEnabled: remoteAccess),
                            id: "info:device-lease:\(device.id)"))
                    }
                    // Drawn only where the daemon stated the health — an
                    // older one publishes no `relay_health`, and a silent
                    // line is the honest answer rather than a green "OK"
                    // nobody measured.
                    if let line = awayHealthLine(
                        device, health: devices.relayHealth) {
                        deviceEntries.append(info(
                            line, id: "info:device-away:\(device.id)"))
                    }
                    // The socket lane's line, drawn only where the daemon
                    // stated its health or a word for this phone.
                    if let line = socketLine(
                        device, health: devices.relayWSHealth) {
                        deviceEntries.append(info(
                            line, id: "info:device-socket:\(device.id)"))
                    }
                    // The grant block. Only where this phone has an away
                    // channel *and* the daemon stated a grant: `leaseDays`
                    // defaults to -1, and an older daemon must draw nothing
                    // rather than a wrong "off". The bot draws its two
                    // grants in this place instead.
                    if let bot = device.botAccess {
                        deviceEntries.append(contentsOf: botAccessRows(
                            deviceId: device.id, access: bot))
                    } else if device.relay && device.leaseDays >= 0 {
                        deviceEntries.append(info(
                            device.leaseDays == 0
                                ? "away access is off for this phone"
                                : "one check-in at home buys "
                                    + grantWords(device.leaseDays),
                            id: "info:device-grant:\(device.id)"))
                        // Deliberately plain rows and not `.pick`: the tick
                        // machinery posts on the menu-bar app's stdin
                        // channel, which writes `preferences.json` and knows
                        // nothing about `relay.json`. The info line above is
                        // what says which length is in force.
                        for days in awayDayChoices {
                            deviceEntries.append(SettingsRow(
                                id: "custom:away-days:\(device.id):\(days)",
                                title: "Grant \(grantWords(days))",
                                kind: .custom(.awayDays(deviceId: device.id,
                                                        days: days)),
                                checked: days == device.leaseDays))
                        }
                        if device.leaseDays > 0 {
                            let ending = awayOffArmed == device.id
                            deviceEntries.append(SettingsRow(
                                id: "custom:away-off:\(device.id)",
                                title: ending
                                    ? "End away access now — really?"
                                    : "End away access now",
                                kind: .custom(.awayOff(deviceId: device.id))))
                        }
                    }
                }
                // The lock-screen switch. Only where the daemon stated it
                // and this phone has an away channel (the buzz travels the
                // relay); an older daemon draws no row.
                if device.relay, let on = device.lockScreenActions {
                    deviceEntries.append(info(
                        on ? "answers from the lock screen: on — a buzz offers Allow, Deny or Acknowledge"
                           : "answers from the lock screen: off — a buzz opens the app",
                        id: "info:device-lock-screen:\(device.id)"))
                    deviceEntries.append(SettingsRow(
                        id: "custom:lock-screen:\(device.id)",
                        title: on ? "Stop answering from the lock screen"
                                  : "Answer from the lock screen",
                        kind: .custom(.lockScreenActions(deviceId: device.id,
                                                         enabled: !on))))
                }
                deviceEntries.append(divider("unpair:\(device.id)"))
                deviceEntries.append(SettingsRow(
                    id: "custom:unpair:\(device.id)",
                    title: armed ? "Un-pair — really?" : "Un-pair",
                    kind: .custom(.unpair(deviceId: device.id))))
                rows.append(submenu(
                    device.name.isEmpty ? device.id : device.name,
                    id: "submenu:device:\(device.id)",
                    rows: deviceEntries))
            }
        }
        if devices.remoteStated && !devices.remoteActivity.isEmpty {
            rows.append(submenu(
                "Done remotely", id: "submenu:remote-activity",
                rows: remoteActivityRows(devices)))
        }
        // The actual bind, not the preference: a failed LAN listen must not
        // offer Pair for a door that is shut. The Phone access toggle stays
        // bound to `lanAccess` so flipping it retries the bind.
        if devices.lanEnabled {
            rows.append(divider("devices-end"))
            rows.append(SettingsRow(
                id: "custom:pairDevice",
                title: "Pair a device…",
                kind: .custom(.pairDevice)))
        }
        return rows
    }

    /// One device's home standing, or nil where there is nothing to say. A
    /// phone paired before the home path was sealed has no home key on this
    /// Mac; the daemon now refuses its every request, and the phone says the
    /// same thing on its own screen. Drawn only where the daemon *stated*
    /// `home_sealed` — an older daemon publishes no `home` key on any device,
    /// and absent must not read as "re-pair everything".
    static func homeLine(_ device: PairedDevice, homeSealed: Bool) -> String? {
        guard homeSealed, !device.home else { return nil }
        return "paired before sealed home access — pair it again"
    }

    /// One device's away standing, in words: how long was granted, and the
    /// moment it closes. The date joins the time once the expiry is not on
    /// today's calendar day — a grant may now be a fortnight out, and a bare
    /// "until 3:14 PM" would name the wrong day. "Lapsed" states plainly that
    /// expiry stops the phone *doing*, never *seeing*.
    /// `remoteEnabled` separates the two reasons a device has no away key.
    /// With the switch off, "no away access" is the plain truth. With it on,
    /// a keyless device is one that was paired *before* the switch was — the
    /// key is minted at pairing and only while away access is on — and the
    /// way out is re-pairing at home, which is worth saying rather than
    /// leaving somebody to wonder why one phone works away and another does
    /// not.
    static func leaseLine(_ device: PairedDevice,
                          remoteEnabled: Bool = false,
                          now: Date = Date()) -> String {
        guard device.relay, device.leaseExpiresAt > 0 else {
            return remoteEnabled
                ? "no away access — re-pair at home to add it"
                : "no away access"
        }
        guard device.leaseExpiresAt > now.timeIntervalSince1970 else {
            return "away access lapsed — reads continue"
        }
        let expiry = Date(timeIntervalSince1970: device.leaseExpiresAt)
        let when = expiryWords(expiry, now: now)
        guard device.leaseDays >= 1 else { return "away access until \(when)" }
        return "\(grantWords(device.leaseDays)) granted — away access until \(when)"
    }

    /// The lengths a grant may be, mirroring the daemon's `LEASE_DAY_CHOICES`.
    /// Shipped in code on both sides: there is no free-text length.
    static let awayDayChoices: [Int] = [1, 3, 7, 14]

    /// One grant length as a person says it.
    static func grantWords(_ days: Int) -> String {
        days == 1 ? "24 hours" : "\(days) days"
    }

    /// The bot's two groups — Read, then Write — each an info line saying
    /// the position in force, the five positions as one row of buttons,
    /// and "Restart the timer" while a timer runs. Plain `.custom` rows and
    /// not `.pick`, for `awayDays`' reason: the tick machinery writes
    /// `preferences.json` and knows nothing about `relay.json`.
    static func botAccessRows(deviceId id: String,
                              access: BotAccess,
                              now: Date = Date()) -> [SettingsRow] {
        var rows: [SettingsRow] = []
        for side in ["read", "write"] {
            let grant = access.grant(side)
            rows.append(info(
                "\(side) access: \(botGrantWords(grant, now: now))",
                id: "info:device-bot-\(side):\(id)"))
            // The segment says the length alone; the side rides the
            // tooltip, which settings search matches and VoiceOver speaks
            // (`SettingsControls.spokenName`), so Read's and Write's
            // buttons never read the same.
            for mode in botAccessModes {
                rows.append(SettingsRow(
                    id: "custom:bot-access:\(id):\(side):\(mode)",
                    title: botModeTitle(mode),
                    kind: .custom(.botAccess(deviceId: id, side: side,
                                             mode: mode)),
                    tooltip: "\(botSideTitle(side)) access: \(botModeTitle(mode))",
                    checked: mode == grant.mode))
            }
            if botTimedModes.contains(grant.mode) {
                rows.append(SettingsRow(
                    id: "custom:bot-restart:\(id):\(side)",
                    title: "Restart the \(side) timer",
                    kind: .custom(.botAccess(deviceId: id, side: side,
                                             mode: grant.mode))))
            }
        }
        return rows
    }

    /// Every position one side of the bot's access can be put in, mirroring
    /// the daemon's `BOT_ACCESS_MODES`.
    static let botAccessModes = ["off", "1h", "6h", "24h", "forever"]

    /// The positions that run on a timer, the daemon's `BOT_ACCESS_SECONDS`.
    static let botTimedModes: Set<String> = ["1h", "6h", "24h"]

    /// One side as a heading says it.
    static func botSideTitle(_ side: String) -> String {
        side == "write" ? "Write" : "Read"
    }

    /// What the settings window says when the daemon refused a change to
    /// the bot's access — its own sentence, or a plain fallback — and nil
    /// when the change landed. Drawn as an alert, the pairing refusal's
    /// shape, so a refused Off is never silent.
    static func botAccessRefusal(_ result: ActionResult) -> String? {
        guard !result.ok else { return nil }
        return result.detail.isEmpty
            ? "Dark Army did not change the bot's access." : result.detail
    }

    /// One position as a button says it.
    static func botModeTitle(_ mode: String) -> String {
        switch mode {
        case "off": return "Off"
        case "1h": return "1 hour"
        case "6h": return "6 hours"
        case "24h": return "24 hours"
        case "forever": return "No timer"
        default: return mode
        }
    }

    /// One grant in words: off, on with no timer, or on until a moment. A
    /// timed grant whose moment has passed reads off before the daemon's
    /// next frame says so.
    static func botGrantWords(_ grant: BotGrant, now: Date = Date()) -> String {
        if grant.mode == "forever" { return "on — no timer" }
        guard botTimedModes.contains(grant.mode),
              grant.until > now.timeIntervalSince1970 else { return "off" }
        let until = Date(timeIntervalSince1970: grant.until)
        return "on until \(expiryWords(until, now: now))"
    }

    /// The moment a window closes. Time alone while it is today, date and
    /// time once it is not — the same rule the phone's `AwaySpan` follows.
    static func expiryWords(_ expiry: Date, now: Date) -> String {
        let formatter = DateFormatter()
        formatter.timeStyle = .short
        formatter.dateStyle = Calendar.current.isDate(expiry, inSameDayAs: now)
            ? .none : .short
        return formatter.string(from: expiry)
    }

    /// One device's away-link line: is it working, and when did it last
    /// carry a message. `nil` where the daemon said nothing about the health
    /// — an older daemon, or a connector that has not polled yet — because
    /// "no line" and "healthy" are different answers.
    ///
    /// The health is machine-wide (one connector, one mailbox) while the
    /// last-carried time is per device, which is why one sentence carries
    /// both: the failing case is about the link, the OK case about this phone.
    static func awayHealthLine(_ device: PairedDevice,
                               health: RelayHealth,
                               now: Date = Date()) -> String? {
        guard health.stated else { return nil }
        if health.isFailing {
            let what = health.status > 0
                ? "status \(health.status)" : "no answer"
            return "away link failing (\(what)) for "
                + relativeSpan(health.failingFor)
        }
        guard device.lastFrameAt > 0 else {
            return "away link OK — nothing carried yet"
        }
        let age = now.timeIntervalSince1970 - device.lastFrameAt
        return "away link OK — last carried a message "
            + relativeSpan(age) + " ago"
    }

    /// One device's socket-lane line, or nil where the daemon said nothing
    /// (`relayWSHealth.stated` false and no `socket` word — an older
    /// daemon, or the lane switched off). The words are the daemon's own
    /// `socket` word per phone; a failing lane names its status and age,
    /// `awayHealthLine`'s shape.
    static func socketLine(_ device: PairedDevice,
                           health: RelayHealth) -> String? {
        guard health.stated || !device.socket.isEmpty else { return nil }
        // The switch is on but there is no line to try: the daemon's own
        // words for it, so the menu says why nothing is connecting.
        if health.state == "no-address" {
            return "socket link: no socket address set"
        }
        if health.state == "bad-address" {
            return "socket link: the socket address must start with wss://"
        }
        if health.isFailing {
            let what = health.status > 0
                ? "status \(health.status)" : "no answer"
            return "socket link failing (\(what)) for "
                + relativeSpan(health.failingFor)
        }
        switch device.socket {
        case "armed": return "socket armed"
        case "open": return "socket open — waiting for the phone"
        case "connecting": return "socket connecting"
        case "off": return health.stated ? "socket off" : nil
        default: return nil
        }
    }

    /// A duration in the coarsest unit that still says something true.
    static func remoteActivityRows(_ devices: Devices) -> [SettingsRow] {
        let names = Dictionary(uniqueKeysWithValues: devices.devices.map {
            ($0.id, $0.name.isEmpty ? $0.id : $0.name)
        })
        let formatter = DateFormatter()
        formatter.timeStyle = .short
        formatter.dateStyle = .none
        // Newest last on the daemon; newest first is what a menu reads.
        return devices.remoteActivity.reversed().enumerated().map { pair in
            let (index, entry) = pair
            let who = names[entry.deviceId] ?? entry.deviceId
            let when = formatter.string(
                from: Date(timeIntervalSince1970: entry.at))
            let verdict = entry.ok ? "" : " — refused"
            return info("\(when)  \(entry.action) · \(who)\(verdict)",
                        id: "info:remote-activity:\(index)")
        }
    }
}
