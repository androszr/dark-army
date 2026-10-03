import Foundation
import CryptoKit
import SwiftUI

/// One pending-tab slot, written by a notification tap or a widget deep
/// link (`onOpenURL`) and consumed by `ContentView` **after** the Face-ID
/// gate — the router never draws anything, so nothing it holds can leak
/// past the lock screen. A slot rather than a call, because both writers
/// routinely fire while the lock view (or nothing at all) is on screen:
/// the tab to show has to wait for the hierarchy that can show it.
@MainActor
final class PhoneRouter: ObservableObject {
    static let shared = PhoneRouter()
    @Published private(set) var destination = DestinationState()
    private var restored = false
    private var discardSavedDestination = false
    private let savedURL: URL
    private let readSaved: (URL) throws -> Data
    private(set) var unlocked = false
    private(set) var requestGeneration = 0
    init(storageURL: URL? = nil, reader: ((URL) throws -> Data)? = nil) {
        savedURL = storageURL ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("pending-notification.json")
        readSaved = reader ?? { try Data(contentsOf: $0) }
    }
    var pendingReceipt: PendingReceipt? { destination.pending }
    var generation: String { destination.generation }
    func accepts(_ route: PendingReceipt) -> Bool { unlocked && destination.accepts(route) }
    func lock() { unlocked = false; requestGeneration += 1 }
    func pair(token: String) {
        let newTap = restored ? nil : destination.pending
        restore()
        let identity = SHA256.hash(data: Data(token.utf8)).map { String(format: "%02x", $0) }.joined()
        destination.pair(identity)
        if discardSavedDestination { destination.pending = nil; discardSavedDestination = false }
        if let newTap { destination.tap(newTap.receiptId) }
        unlocked = true
        persist()
        signal += 1
    }
    private func restore() {
        guard !restored else { return }
        do {
            let data = try readSaved(savedURL)
            destination = try JSONDecoder().decode(DestinationState.self, from: data)
            restored = true
        } catch let error as CocoaError where error.code == .fileReadNoSuchFile {
            restored = true
        } catch { /* Protected data may be unavailable until the next unlock. */ }
    }
    func tap(receipt: String) {
        guard UUID(uuidString: receipt) != nil else { go(.needs); return }
        restore()
        discardSavedDestination = false
        destination.tap(receipt.lowercased())
        if !unlocked { destination.pending?.generation = "" }
        persist()
        signal += 1
    }
    func consume(_ route: PendingReceipt) { destination.consume(route); persist() }
    func forget() {
        lock()
        destination = DestinationState()
        try? FileManager.default.removeItem(at: savedURL)
        restored = true
    }
    private func persist() {
        guard restored, let data = try? JSONEncoder().encode(destination) else { return }
        do {
            try FileManager.default.createDirectory(at: savedURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: savedURL, options: [.atomic, .completeFileProtection])
        } catch { /* A locked protected file is retried after unlock. */ }
    }

    /// The tab a tap asked for. Consumed on read (`take()`), so a tab the
    /// user has since navigated away from is never re-forced by a stale
    /// signal.
    @Published private(set) var pendingTab: PhoneTab?
    /// Bumped on every `go`, so two taps on the same destination still
    /// re-apply — equality on `pendingTab` alone would swallow the second.
    @Published private(set) var signal = 0

    func go(_ tab: PhoneTab) {
        restore()
        pendingTab = tab
        discardSavedDestination = !restored
        destination.pending = nil
        persist()
        signal += 1
    }

    /// A `bobphone://<tab>` deep link. An unknown host routes nowhere —
    /// never a guess. `?session=<id>` (the widget's face, `FleetLinks.agent`)
    /// asks for that agent's sheet once the tab is in front; it is held in
    /// memory only and consumed with the tab, behind the face check like
    /// everything else a link can aim.
    func open(_ url: URL) {
        guard url.scheme == FleetLinks.scheme,
              let tab = PhoneTab(stored: url.host ?? "") else { return }
        // `bobphone://usage` names a Menu section: the tab comes forward
        // and the section opens on it.
        pendingSection = MenuSection(rawValue: url.host ?? "")
        pendingSession = URLComponents(url: url, resolvingAgainstBaseURL: false)?
            .queryItems?.first { $0.name == "session" }?.value ?? ""
        go(tab)
    }

    /// The agent a deep link named, `""` for none. Memory only.
    private var pendingSession = ""

    /// The Menu section a deep link named, `nil` for none. Memory only,
    /// consumed with the tab like the session.
    private var pendingSection: MenuSection?

    /// The Review run a Needs you press named, `""` for none. Memory only,
    /// consumed by `ReviewView` once it is in front.
    private var pendingReviewRun = ""

    /// A Needs you entry for a run waiting on picks: the Menu tab comes
    /// forward with the Review screen open and the run to show held for it.
    /// Posts nothing — it only moves the screen.
    func openReview(runId: String) {
        pendingSection = .review
        pendingReviewRun = runId
        go(.menu)
    }

    /// Consume the run a press named, if any.
    func takeReviewRun() -> String {
        defer { pendingReviewRun = "" }
        return pendingReviewRun
    }

    /// Consume the section a link named, if any.
    func takeSection() -> MenuSection? {
        defer { pendingSection = nil }
        return pendingSection
    }

    /// Consume the slot. Called by `ContentView` once it exists and is in
    /// front of an unlocked screen.
    func take() -> PhoneTab? {
        defer { pendingTab = nil }
        return pendingTab
    }

    /// Consume the agent a link named, if any.
    func takeSession() -> String {
        defer { pendingSession = "" }
        return pendingSession
    }
}
