import AppKit
import Combine
import CoreImage
import CoreImage.CIFilterBuiltins
import SwiftUI

/// QR of `{host, hosts, port, code}` plus a two-minute countdown. Closes on a
/// successful pair (a new device appears in the snapshot) or when the code
/// expires.
///
/// The tick under the caption arms the daemon's typed-address pair branch
/// for **one** pairing: flipping it calls `onRestart` with the new value,
/// which begins a fresh code through the loopback `begin_pairing` and
/// swaps it into this same window. Its state is `session.allowsTyping` —
/// the daemon's own echo of what it stored — so a stale or older reply
/// draws the tick off rather than claiming a permission it never granted.
/// While armed, the code is drawn as text so it can be read into the phone.
struct PairingView: View {
    @ObservedObject var client: DaemonClient
    let session: PairingSession
    let onClose: () -> Void
    let onRestart: (Bool) -> Void

    private var typingBinding: Binding<Bool> {
        Binding(
            get: { session.allowsTyping },
            set: { on in
                if on != session.allowsTyping { onRestart(on) }
            })
    }

    var body: some View {
        VStack(spacing: 16) {
            Text("Pair a device")
                .font(Theme.mono(16, weight: .semibold))
                .foregroundStyle(Theme.phosphor)
            if let image = session.qrImage {
                Image(nsImage: image)
                    .interpolation(.none)
                    .resizable()
                    .scaledToFit()
                    .frame(width: 220, height: 220)
                    .padding(8)
                    .background(Color.white)
            }
            Text(session.hostPort)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
            Text(session.remainingLabel)
                .font(Theme.mono(13, weight: .medium))
                .foregroundStyle(session.expired ? Theme.alarm : Theme.phosphor)
            Text("Scan with the Dark Army phone app. The code works once, for two minutes.")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 280)
            Toggle("This phone can't scan — allow typing the address",
                   isOn: typingBinding)
                .toggleStyle(.checkbox)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .frame(maxWidth: 280)
            if session.allowsTyping {
                Text(session.code)
                    .font(Theme.mono(20, weight: .semibold))
                    .foregroundStyle(Theme.phosphor)
                    .textSelection(.enabled)
                Text("Type the address and this code into the phone. "
                     + "A new code was issued; nothing secret crosses this "
                     + "Wi-Fi — the phone proves it knows the code and both "
                     + "sides derive the key.")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 280)
            }
            Button("Close") { onClose() }
                .keyboardShortcut(.cancelAction)
                .font(Theme.mono(12))
        }
        .padding(24)
        .frame(minWidth: 320, minHeight: 470)
        .background(Theme.bg)
        .preferredColorScheme(.dark)
        .onReceive(session.tick) { _ in
            session.refresh()
            if session.expired { onClose() }
        }
        .onChange(of: client.snapshot.devices.devices.map(\.id)) { _, ids in
            if session.paired(ids) { onClose() }
        }
        .onChange(of: client.snapshot.devices.pairingOpen) { _, open in
            if session.notePairingOpen(open) { onClose() }
        }
        .onAppear {
            if session.notePairingOpen(client.snapshot.devices.pairingOpen) {
                onClose()
            }
        }
    }
}

@MainActor
final class PairingSession: ObservableObject {
    let code: String
    let host: String
    /// Every address the phone may try, best first — LAN addresses then
    /// the Bonjour name. A tunnel address is never among them: the Mac's
    /// door closes a knock on one unanswered, so it is not worth a try.
    let hosts: [String]
    let port: Int
    let expiresAt: Double
    /// The pairing's home key, base64 — rides the QR and nothing else, so a
    /// phone that scans the square seals its very first request under it and
    /// the key never crosses the Wi-Fi. Empty on an older daemon.
    let homeKey: String
    /// Whether the daemon armed this pairing for a typed-address pair — the
    /// tick's state, read off the daemon's echo and never derived here.
    let allowsTyping: Bool
    let knownIds: Set<String>
    let qrImage: NSImage?
    let tick = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    @Published var remaining: Int = 0
    /// A true we have actually seen. A stale ``pairing_open: false`` that
    /// arrives just after present must not close the square we just opened.
    private var sawOpen = false

    init(result: PairingResult, knownIds: [String]) {
        code = result.code
        host = result.host
        let list = result.hosts.isEmpty
            ? (result.host.isEmpty ? [] : [result.host])
            : result.hosts
        hosts = list
        port = result.port
        expiresAt = result.expiresAt
        homeKey = result.homeKey
        allowsTyping = result.allowTyped
        self.knownIds = Set(knownIds)
        qrImage = Self.makeQR(hosts: list, port: result.port, code: result.code,
                              homeKey: result.homeKey)
        remaining = Self.secondsLeft(expiresAt)
    }

    var expired: Bool { remaining <= 0 }
    /// The address the phone will try first, and how many more are behind
    /// it — the count is what tells somebody staring at a failed pair that
    /// the Mac offered alternatives at all.
    var hostPort: String {
        guard let first = hosts.first, !first.isEmpty else { return "port \(port)" }
        let extra = hosts.count - 1
        return extra > 0 ? "\(first):\(port) +\(extra) more" : "\(first):\(port)"
    }
    var remainingLabel: String {
        if expired { return "Code expired" }
        let minutes = remaining / 60
        let seconds = remaining % 60
        return String(format: "%d:%02d left", minutes, seconds)
    }

    func refresh() {
        remaining = Self.secondsLeft(expiresAt)
    }

    func paired(_ ids: [String]) -> Bool {
        !Set(ids).subtracting(knownIds).isEmpty
    }

    /// Latch: ``true`` then ``false`` means the daemon burned the code.
    /// Returns whether the square should close.
    func notePairingOpen(_ open: Bool) -> Bool {
        if open {
            sawOpen = true
            return false
        }
        return sawOpen
    }

    private static func secondsLeft(_ expiresAt: Double) -> Int {
        max(0, Int(expiresAt - Date().timeIntervalSince1970))
    }

    /// The loopback request that begins a pairing. `["action":
    /// "begin_pairing"]` alone when typing is off — **byte-identical** to
    /// what every earlier build sent, so an older daemon sees nothing new —
    /// and `allow_typed: true` beside it when the tick is on. The seam
    /// `PairingPayloadTests` pins.
    static func beginPayload(allowTyped: Bool) -> [String: Any] {
        var payload: [String: Any] = ["action": "begin_pairing"]
        if allowTyped {
            payload["allow_typed"] = true
        }
        return payload
    }

    /// The bytes the QR carries. Split out from `makeQR` because a
    /// `CIImage` is not something a test can read back — this is the seam
    /// `PairingPayloadTests` decodes.
    ///
    /// `host` is kept beside `hosts` deliberately: a phone build from before
    /// the ordered list reads only `host`, and dropping it would unpair
    /// every installed app for the price of a few QR bytes.
    ///
    /// `home_key` is added **only when non-empty**: an older daemon's QR and
    /// the existing payload cases stay byte-identical, and a phone that
    /// finds no key falls back to the plain pair the Mac still answers.
    static func qrPayload(hosts: [String], port: Int, code: String,
                          homeKey: String = "") -> Data? {
        var payload: [String: Any] = [
            "host": hosts.first ?? "",
            "hosts": hosts,
            "port": port,
            "code": code,
        ]
        if !homeKey.isEmpty {
            payload["home_key"] = homeKey
        }
        return try? JSONSerialization.data(withJSONObject: payload)
    }

    private static func makeQR(hosts: [String], port: Int, code: String,
                               homeKey: String) -> NSImage? {
        guard let data = qrPayload(hosts: hosts, port: port, code: code,
                                   homeKey: homeKey) else {
            return nil
        }
        let filter = CIFilter.qrCodeGenerator()
        filter.message = data
        filter.correctionLevel = "M"
        guard let output = filter.outputImage else { return nil }
        let scaled = output.transformed(by: CGAffineTransform(scaleX: 10, y: 10))
        let rep = NSCIImageRep(ciImage: scaled)
        let image = NSImage(size: rep.size)
        image.addRepresentation(rep)
        return image
    }
}

@MainActor
final class PairingWindowController: NSObject, NSWindowDelegate {
    /// The QR window currently on screen, if any. The panel's hide/close
    /// paths call `forceClose` through this rather than holding a second
    /// window manager — the coordinator still owns the object.
    static weak var current: PairingWindowController?

    private let client: DaemonClient
    private var window: NSWindow?
    private var session: PairingSession?

    init(client: DaemonClient) {
        self.client = client
        super.init()
    }

    /// `onRestart` is called with the tick's new value when the person
    /// flips it; the caller begins a fresh pairing and calls `present`
    /// again, which swaps this same window's content.
    func present(_ result: PairingResult,
                 onRestart: @escaping (Bool) -> Void = { _ in }) {
        PairingWindowController.current = self
        let known = client.snapshot.devices.devices.map(\.id)
        let session = PairingSession(result: result, knownIds: known)
        self.session = session
        let root = PairingView(client: client, session: session,
                               onClose: { [weak self] in self?.close() },
                               onRestart: onRestart)
        let hosting = NSHostingController(rootView: root)
        if let window {
            window.contentViewController = hosting
            window.makeKeyAndOrderFront(nil)
            return
        }
        let window = NSWindow(contentViewController: hosting)
        window.title = "Pair a device"
        window.styleMask = [.titled, .closable]
        window.setContentSize(NSSize(width: 360, height: 520))
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.backgroundColor = Theme.nsBg
        window.appearance = NSAppearance(named: .darkAqua)
        window.center()
        window.makeKeyAndOrderFront(nil)
        self.window = window
    }

    /// Whether this key press belongs to the pairing window. Identity
    /// against our own window; a nil window is not ours by this test —
    /// `isKey` is the total one that covers it. Card window's shape.
    func owns(_ candidate: NSWindow?) -> Bool {
        guard let candidate, let window else { return false }
        return candidate === window
    }

    var isKey: Bool { window?.isKeyWindow == true }

    func close() {
        window?.orderOut(nil)
        if PairingWindowController.current === self {
            PairingWindowController.current = nil
        }
    }

    /// Panel teardown: take it off screen the same way `hide` takes the
    /// card window. The object is reused for the next Pair.
    func forceClose() {
        close()
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        if PairingWindowController.current === self {
            PairingWindowController.current = nil
        }
        return false
    }
}
