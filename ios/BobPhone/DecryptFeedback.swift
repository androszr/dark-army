import SwiftUI
import UIKit

@MainActor
final class DecryptFeedback: ObservableObject {
    @Published private(set) var state = DecryptMotion.State()
    @Published private(set) var instant: Double = 0
    private(set) var reduced = false
    private var task: Task<Void, Never>?
    private let now: () -> Double
    private let schedules: Bool

    init(now: @escaping () -> Double = { ProcessInfo.processInfo.systemUptime }, schedules: Bool = true) {
        self.now = now; self.schedules = schedules
    }
    func arrive(_ surface: String) {
        guard state.surface != surface else { return }
        state.arrive(surface, at: now()); restart()
    }
    func begin(kind: DecryptMotion.Kind, surface: String? = nil) {
        guard let surface = surface ?? state.surface else { return }
        state.begin(kind, surface: surface, at: now()); restart()
    }
    /// A native picker can dismiss before or after the page's UIKit arrival.
    /// Both routes mean the same return, including Cancel with no selection.
    func returnedFromPresentation() {
        state.returnedFromPresentation(at: now()); restart()
    }
    func activate(enabled: Bool = true, action: () -> Void) {
        guard enabled else { return }
        action()
    }
    func cancel(surface: String? = nil) {
        guard surface == nil || state.surface == surface else { return }
        task?.cancel(); task = nil; state.cancel(surface: surface)
    }
    func setReduced(_ value: Bool) {
        guard reduced != value else { return }
        reduced = value; restart()
    }
    func advance() { instant = now(); state.tick(at: instant) }
    private func restart() {
        task?.cancel(); advance()
        guard schedules else { return }
        task = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                self.advance()
                guard let next = self.state.nextTick(at: self.instant, reduced: self.reduced) else { return }
                do { try await Task.sleep(for: .seconds(max(0, next - self.now()))) }
                catch { return }
                guard !Task.isCancelled else { return }
            }
        }
    }
}

/// Where a sheet draws the arrival caption for the surface inside it. The
/// sheet frame owns one and puts it in its content's environment; a
/// `DecryptSurface` under it publishes its identity here on arrival and mounts
/// no strip of its own, so a sheet reserves no band under its title — the
/// caption plays in the frame's header row instead. Tab roots have no host
/// and keep their strip exactly as before.
@MainActor
final class DecryptCaptionHost: ObservableObject {
    @Published var surface: String?
}

private struct DecryptOwnerKey: EnvironmentKey { static let defaultValue: DecryptFeedback? = nil }
private struct DecryptCaptionHostKey: EnvironmentKey { static let defaultValue: DecryptCaptionHost? = nil }
private struct DecryptActiveKey: EnvironmentKey { static let defaultValue = true }
extension EnvironmentValues {
    var decryptFeedback: DecryptFeedback? {
        get { self[DecryptOwnerKey.self] } set { self[DecryptOwnerKey.self] = newValue }
    }
    var decryptActive: Bool {
        get { self[DecryptActiveKey.self] } set { self[DecryptActiveKey.self] = newValue }
    }
    var decryptCaptionHost: DecryptCaptionHost? {
        get { self[DecryptCaptionHostKey.self] } set { self[DecryptCaptionHostKey.self] = newValue }
    }
}

/// Wraps the native Button's action, not its gesture: keyboard and VoiceOver
/// activate the same closure; disabled and drag-off presses stay native no-ops.
struct DecryptButton<Label: View>: View {
    @Environment(\.decryptFeedback) private var feedback
    @Environment(\.isEnabled) private var enabled
    let role: ButtonRole?
    let action: () -> Void
    let label: Label
    init(role: ButtonRole? = nil, action: @escaping () -> Void, @ViewBuilder label: () -> Label) {
        self.role = role; self.action = action; self.label = label()
    }
    init(_ title: String, role: ButtonRole? = nil, action: @escaping () -> Void) where Label == Text {
        self.init(role: role, action: action) { Text(title) }
    }
    var body: some View {
        Button(role: role) {
            if let feedback { feedback.activate(enabled: enabled, action: action) }
            else if enabled { action() }
        } label: { label }
    }
}

/// Final text reserves the geometry; projected glyphs never alter layout.
struct DecryptCaption: View {
    let caption: String
    let frame: String?
    var body: some View {
        Text(caption).hidden()
            .overlay(alignment: .leading) { Text(frame ?? "").foregroundStyle(Theme.phosphorBright) }
            .font(Theme.mono(11, weight: .medium))
            .fixedSize(horizontal: false, vertical: true)
            .accessibilityHidden(true)
            .allowsHitTesting(false)
    }
}

private struct DecryptChrome: View {
    @ObservedObject var feedback: DecryptFeedback
    let surface: String
    let active: Bool
    var body: some View {
        let visible = active && feedback.state.surface == surface
        let glyphs = visible ? feedback.state.screen?.frame(at: feedback.instant, reduced: feedback.reduced) : nil
        // Always laid out, so the strip's height is reserved whether or not
        // a scramble is playing: `DecryptCaption` holds the geometry with
        // hidden text and the rule dims to `Theme.rule` when idle. The
        // surface mounts this as a top safe-area inset, never an overlay —
        // an overlay painted the line across the content's first row.
        HStack {
            DecryptCaption(caption: "OPEN", frame: glyphs)
            Spacer(minLength: 4)
        }
        .padding(.horizontal, 12).padding(.vertical, 3)
        .background(Theme.bar)
        .overlay(alignment: .bottom) {
            Rectangle().fill(glyphs != nil ? Theme.phosphor : Theme.rule).frame(height: 1)
        }
        .accessibilityHidden(true).allowsHitTesting(false)
    }
}

/// UIKit reports completed arrivals, including back navigation. Unlike
/// onAppear, this does not commit the destination of a cancelled back swipe.
private struct DecryptAppearance: UIViewControllerRepresentable {
    let arrived: () -> Void
    let departed: () -> Void
    func makeUIViewController(context: Context) -> Controller {
        let controller = Controller()
        controller.arrived = arrived; controller.departed = departed
        return controller
    }
    func updateUIViewController(_ controller: Controller, context: Context) {
        controller.arrived = arrived; controller.departed = departed
    }
    final class Controller: UIViewController {
        var arrived: (() -> Void)?
        var departed: (() -> Void)?
        override func loadView() { view = UIView(); view.isUserInteractionEnabled = false }
        override func viewDidAppear(_ animated: Bool) { super.viewDidAppear(animated); arrived?() }
        override func viewDidDisappear(_ animated: Bool) { super.viewDidDisappear(animated); departed?() }
    }
}

private struct DecryptSurface: ViewModifier {
    let name: String
    @Environment(\.decryptFeedback) private var feedback
    @Environment(\.decryptActive) private var active
    @Environment(\.decryptCaptionHost) private var host
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var reduced
    @State private var appeared = false
    @State private var identity = UUID().uuidString
    func body(content: Content) -> some View {
        content
            .background {
                DecryptAppearance(arrived: {
                    appeared = true
                    host?.surface = identity
                    if active && scenePhase == .active { feedback?.setReduced(reduced); feedback?.arrive(identity) }
                }, departed: {
                    appeared = false; releaseHost(); feedback?.cancel(surface: identity)
                }).allowsHitTesting(false).accessibilityHidden(true)
            }
            // Inside a sheet the frame's header draws the caption (`host`),
            // so the strip is mounted only where no host is present.
            .safeAreaInset(edge: .top, spacing: 0) {
                if let feedback, host == nil { DecryptChrome(feedback: feedback, surface: identity, active: active) }
            }
            .onChange(of: active) { _, value in
                if value && appeared && scenePhase == .active { feedback?.arrive(identity) }
                else if !value { feedback?.cancel(surface: identity) }
            }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active && active && appeared { feedback?.arrive(identity) }
                else { feedback?.cancel(surface: identity) }
            }
            .onChange(of: reduced) { _, value in feedback?.setReduced(value) }
            .onDisappear { releaseHost(); feedback?.cancel(surface: identity) }
    }

    /// Clears the host only while it still names this surface: the next
    /// sheet's content may already have arrived and published its own.
    private func releaseHost() {
        if host?.surface == identity { host?.surface = nil }
    }
}

extension View {
    func decryptSurface(_ name: String) -> some View { modifier(DecryptSurface(name: name)) }
}

extension Binding {
    /// Passthrough. Pickers and disclosures no longer start an INPUT scramble.
    /// Call sites keep the helper so the inventory stays explicit; polls and
    /// restored drafts never did pass through here.
    func decrypting(_ feedback: DecryptFeedback?, kind: DecryptMotion.Kind = .button) -> Binding<Value> {
        _ = feedback
        _ = kind
        return self
    }
}

/// Native interaction adapter for the byte-identical area picker.
struct AreaChoiceButton<Label: View>: View {
    let action: () -> Void
    let label: Label
    init(action: @escaping () -> Void, @ViewBuilder label: () -> Label) {
        self.action = action
        self.label = label()
    }
    var body: some View {
        DecryptButton(action: action) { label }
            .buttonStyle(.plain)
    }
}
