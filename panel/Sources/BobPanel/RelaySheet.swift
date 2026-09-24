import AppKit
import SwiftUI

/// The relay-address window — the mailbox the away path uses, its push
/// secret, and the socket relay's address for the trial Socket link —
/// `PairingWindowController`'s small-window shape. It posts the loopback
/// `set_relay` then `set_relay_ws` actions and shows the daemon's refusal
/// **verbatim** (for instance the non-`https://` or non-`wss://` sentence):
/// the daemon is the authority on what a usable address is, and two
/// wordings of one refusal is a drift.
struct RelaySheetView: View {
    @ObservedObject var client: DaemonClient
    let onClose: () -> Void

    @State private var address = ""
    @State private var socketAddress = ""
    @State private var pushSecret = ""
    @State private var refusal = ""
    @State private var saving = false
    @State private var loaded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Relay address")
                .font(Theme.mono(16, weight: .semibold))
                .foregroundStyle(Theme.phosphor)
            Text("The mailbox the phone uses away from home — your own "
                 + "Vercel deployment of relay/. It carries only sealed "
                 + "envelopes it cannot read.")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
            Text("A phone copies this address at pairing time. Changing it "
                 + "here strands any phone already paired — re-pair each one "
                 + "at home afterwards.")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
            TextField("https://…", text: $address)
                .textFieldStyle(.roundedBorder)
                .font(Theme.mono(12))
                .disableAutocorrection(true)
            Text("Push secret — the PUSH_SECRET your deployment holds. It "
                 + "lets only this Mac send phone buzzes through the relay. "
                 + "Leave blank to keep the one already stored; it is never "
                 + "shown back.")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
            SecureField("PUSH_SECRET", text: $pushSecret)
                .textFieldStyle(.roundedBorder)
                .font(Theme.mono(12))
                .disableAutocorrection(true)
            Text("Socket address — the socket relay (relay-ws/ on Fly.io) "
                 + "the trial Socket link uses beside the mailbox. A phone "
                 + "copies it at pairing time while Socket link is on; a "
                 + "phone paired before, or while the link was off, needs "
                 + "pairing again at home. Leave blank for no socket.")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
            TextField("wss://…", text: $socketAddress)
                .textFieldStyle(.roundedBorder)
                .font(Theme.mono(12))
                .disableAutocorrection(true)
            if !refusal.isEmpty {
                Text(refusal)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.alarm)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Spacer()
                Button("Cancel") { onClose() }
                    .keyboardShortcut(.cancelAction)
                    .font(Theme.mono(12))
                Button(saving ? "Saving…" : "Save") { save() }
                    .keyboardShortcut(.defaultAction)
                    .font(Theme.mono(12))
                    .disabled(saving)
            }
        }
        .padding(24)
        .frame(width: 420)
        .background(Theme.bg)
        .preferredColorScheme(.dark)
        .onAppear {
            guard !loaded else { return }
            loaded = true
            address = client.snapshot.devices.relayURL
            socketAddress = client.snapshot.devices.relayWSURL
        }
    }

    private func save() {
        saving = true
        refusal = ""
        Task {
            // The mailbox first, then the socket: either refusal is the
            // daemon's own sentence, shown verbatim, and a refused socket
            // address leaves the mailbox address already saved.
            let result = await client.setRelay(
                address.trimmingCharacters(in: .whitespacesAndNewlines),
                pushSecret: pushSecret.trimmingCharacters(
                    in: .whitespacesAndNewlines))
            guard result.ok else {
                saving = false
                refusal = result.detail.isEmpty
                    ? "The daemon did not answer." : result.detail
                return
            }
            let socket = await client.setRelayWS(
                socketAddress.trimmingCharacters(in: .whitespacesAndNewlines))
            saving = false
            if socket.ok {
                onClose()
            } else {
                refusal = socket.detail.isEmpty
                    ? "The daemon did not answer." : socket.detail
            }
        }
    }
}

@MainActor
final class RelaySheetController: NSObject, NSWindowDelegate {
    private let client: DaemonClient
    private var window: NSWindow?

    init(client: DaemonClient) {
        self.client = client
        super.init()
    }

    func present() {
        let root = RelaySheetView(client: client) { [weak self] in
            self?.close()
        }
        let hosting = NSHostingController(rootView: root)
        if let window {
            window.contentViewController = hosting
            window.makeKeyAndOrderFront(nil)
            return
        }
        let window = NSWindow(contentViewController: hosting)
        window.title = "Relay address"
        window.styleMask = [.titled, .closable]
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.backgroundColor = Theme.nsBg
        window.appearance = NSAppearance(named: .darkAqua)
        window.center()
        window.makeKeyAndOrderFront(nil)
        self.window = window
    }

    func close() {
        window?.orderOut(nil)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }
}
