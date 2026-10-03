import SwiftUI

/// The Menu tab's Rebuild & restart screen: the Mac's current build line and
/// one button. A first press arms it ("Really rebuild and restart?"), a second
/// sends `rebuild_app` through `post` — never `enqueue`, so a rebuild is never
/// banked and fired later on the phone's clock. Away it is offered only where
/// the Mac says it accepts an away press; otherwise the button is absent and the
/// screen says why. `RebuildRules` owns every word.
struct RebuildView: View {
    @ObservedObject var client: PhoneClient
    @StateObject private var arm = Arm()
    @State private var pressedAt: Double?
    @State private var note = ""
    @State private var sending = false

    private var section: RebuildSection { client.snapshot.rebuild }
    private var away: Bool { client.via == .relay }
    private var awayAllowed: Bool { client.snapshot.board.rebuildAwaySupported }
    private var connected: Bool { client.status == .live }

    private var status: String {
        RebuildRules.line(section: section, connected: connected, away: away,
                          awayAllowed: awayAllowed, pressedAt: pressedAt)
    }

    private var failed: Bool {
        RebuildRules.isFailure(section: section, pressedAt: pressedAt)
    }

    private var canPress: Bool {
        RebuildRules.canPress(section: section, away: away,
                              awayAllowed: awayAllowed)
            && connected && !sending
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                PromptLine(path: "~/rebuild")
                Text(status)
                    .font(Theme.mono(13))
                    .foregroundStyle(failed ? Theme.alarm : Theme.phosphor)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel(status)
                if arm.rebuild != nil {
                    Text(away ? RebuildRules.awayArmedWarning : RebuildRules.armedWarning)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.alarm)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if RebuildRules.offered(section: section, away: away,
                                         awayAllowed: awayAllowed) {
                    DecryptButton(arm.rebuild != nil
                                  ? RebuildRules.armedLabel
                                  : RebuildRules.idleLabel) {
                        press()
                    }
                    .buttonStyle(AlarmOutline(
                        color: arm.rebuild != nil ? Theme.alarm : Theme.phosphor))
                    .disabled(!canPress)
                    .accessibilityLabel(arm.rebuild != nil
                        ? "Confirm rebuild and restart, step two of two"
                        : "Rebuild and restart the Mac's Dark Army, step one of two")
                }
                if !note.isEmpty {
                    Text(note)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.alarm)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .onDisappear { arm.disarm() }
    }

    private func press() {
        guard arm.confirm(.rebuild) else {
            arm.arm(.rebuild)
            return
        }
        note = ""
        sending = true
        let at = Date().timeIntervalSince1970
        Task { @MainActor in
            let result = await client.post(action: PhoneActions.rebuildApp)
            sending = false
            if result.ok {
                pressedAt = at
            } else if client.status == .live {
                // A refusal in the Mac's own words. A transport failure
                // while the link drops is the restart, not a refusal: the
                // reconnect bar already says the link is down.
                note = result.detail
            }
        }
    }
}
