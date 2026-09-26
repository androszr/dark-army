import SwiftUI
import UIKit

/// The pushed `~/profile` screen: what this phone is, which Mac it talks to,
/// and whether that Mac is answering. Read-only apart from **Forget this
/// Mac**, which moved here from the top bar's old `⋯` menu — this screen is
/// now the app's only way to un-pair, so it lands in the same change that
/// removed the menu.
///
/// `UsageView`'s shape: a plain `List` on `Theme.bg`, `PhoneSectionHeader`
/// headings, `Theme.mono` everywhere, dark-only, no decorative animation.
/// The one clock is the last-heard row, isolated in its own `TimelineView`
/// exactly as `StaleBanner` is, so nothing else re-renders once a second.
///
/// It shows the pairing's addresses and identity, and never its secret.
/// The device name is `UIDevice.current.name` live — `PairingRecord` stores
/// no name to prefer (iOS 16+ may answer a generic "iPhone" without the
/// entitlement; acceptable, it is identity garnish).
struct ProfileView: View {
    @Environment(\.decryptFeedback) private var decryptFeedback
    @State private var widgetMinutes = BackgroundRefresh.minutes
    @ObservedObject var client: PhoneClient
    @ObservedObject var pairing: PairingStore
    @ObservedObject var away: AwayState = .shared
    /// Every press that has not been seen to have happened. Observed
    /// directly rather than through `client` so the list redraws when a
    /// receipt moves and for no other reason.
    @ObservedObject var receipts: ReceiptLedger
    let onForget: () -> Void

    @State private var confirmingForget = false
    @State private var confirmingKeyRemoval = false
    /// The key being typed, cleared the moment it is saved. Never read back
    /// from the store into this field.
    @State private var keyDraft = ""
    /// The store's answer, re-read after every save and removal; the row
    /// draws from this rather than asking the Keychain on every redraw.
    @State private var keySuffix = AnthropicKeyStore.suffix
    @State private var replacingKey = false
    /// The receipt id whose RETRY is out. Keyed on the id *pressed* and
    /// cleared unconditionally, never compared against the ledger
    /// afterwards: `retryReceipt` mints a fresh id, so the row's identity
    /// changes on completion and a lookup would never match.
    @State private var retryingReceipt = ""
    /// The bot's two positions as the menus hold them, seeded from the
    /// Mac's published grant and re-seeded whenever it moves, so a change
    /// made at the desk wins. A differing choice is what posts.
    @State private var botReadMode = ""
    @State private var botWriteMode = ""
    /// The side (`read` / `write`) whose press is out, or empty.
    @State private var botSending = ""
    /// Sides whose menu this screen is moving itself (a re-seed from the
    /// Mac's picture, or putting it back after a refusal). The next change
    /// on such a side is ours, never the person's, and never posts.
    @State private var botReseeding: Set<String> = []
    /// The Mac's own words for the last refused press, per side, drawn
    /// under that side's line until the next press.
    @State private var botRefusal: [String: String] = [:]

    /// `client.status` spelled as a word a person can read.
    private var statusWord: String {
        switch client.status {
        case .idle: return "idle"
        case .connecting: return "connecting"
        case .live: return "live"
        case .unpaired: return "no longer paired"
        case .unreachable: return "unreachable"
        }
    }

    var body: some View {
        List {
            PhoneSectionHeader(title: "THIS PHONE")
                .profileRow()
            row(label: "name", value: UIDevice.current.name)
                .profileRow()

            // `promote` rewrites `record.host` to whichever address actually
            // answered, so showing it *is* showing the live polling address.
            if let record = pairing.record {
                PhoneSectionHeader(title: "PAIRED MAC")
                    .profileRow()
                row(label: "address", value: "\(record.host):\(record.port)")
                    .profileRow()
                ForEach(record.hosts.filter { $0 != record.host }, id: \.self) { spare in
                    row(label: "fallback", value: "\(spare):\(record.port)", faint: true)
                        .profileRow()
                }
                if !record.deviceId.isEmpty {
                    row(label: "device id", value: record.deviceId, faint: true)
                        .profileRow()
                }
                // Stated once, faintly, and only for a record an older
                // version of this app made by typing the address: no route
                // of this version sends the key over the Wi-Fi, so pairing
                // again — by scanning or by typing — replaces that key.
                if record.homeKeyCrossedInClear {
                    row(label: "home key",
                        value: "crossed the Wi-Fi in the clear when an older "
                            + "version of this app paired — pair again, by "
                            + "scanning or by typing, to replace it",
                        faint: true)
                        .profileRow()
                }
            }

            // The away window, drawn only where there is one. A zero or
            // absent expiry is no section at all — never "expired", which
            // would be a claim about a window that may simply not exist.
            // Read from the Mac's published number through the same
            // formatter the top bar uses; nothing is derived twice.
            if away.leaseExpiresAt > 0, let line = AwaySpan.line(
                leaseExpiresAt: away.leaseExpiresAt, via: away.via,
                now: Date().timeIntervalSince1970) {
                PhoneSectionHeader(title: "AWAY")
                    .profileRow()
                row(label: "window",
                    value: line.text.replacingOccurrences(of: "// ", with: ""))
                    .profileRow()
                // The socket lane's word — connecting or open — while a
                // line is wanted; absent otherwise. A word, never an address.
                if !away.socket.isEmpty {
                    row(label: "socket", value: away.socket)
                        .profileRow()
                }
            }

            botSection

            PhoneSectionHeader(title: "CONNECTION")
                .profileRow()
            row(label: "status", value: statusWord)
                .profileRow()
            if !client.lastError.isEmpty {
                row(label: "last error", value: client.lastError, faint: true)
                    .profileRow()
            }
            if let heard = client.lastHeard {
                lastHeardRow(heard)
                    .profileRow()
            }

            pendingSection

            PhoneSectionHeader(title: "WIDGET")
                .profileRow()
            // The tile's own clock: how often the app checks in for it with
            // nobody watching. A floor iOS may stretch, and the row says so.
            Picker(selection: $widgetMinutes.decrypting(decryptFeedback)) {
                ForEach(BackgroundRefresh.intervals, id: \.self) { minutes in
                    Text("\(minutes) min").tag(minutes)
                }
            } label: {
                Text("earliest refresh")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.faint)
            }
            .font(Theme.mono(12))
            .tint(Theme.phosphor)
            .padding(.horizontal, 14)
            .padding(.vertical, 6)
            .profileRow()
            .onChange(of: widgetMinutes) { _, minutes in
                BackgroundRefresh.minutes = minutes
            }
            row(label: "note", value: "The chosen interval is the earliest requested background refresh. iOS decides when updates arrive; timing is not guaranteed.",
                faint: true)
                .profileRow()
            // What the tile is actually fed, read off the phone itself: the
            // note on disk and its age, the last write's outcome, the last
            // reload asked of WidgetKit and the counted ones spent. A tile
            // sat on a day-old note for a day before anything could say
            // which of the three had stopped.
            ForEach(WidgetDiagnostics.rows(client: client), id: \.0) { label, value in
                row(label: label, value: value)
                    .profileRow()
            }

            prepareSection

            if client.snapshot.board.knowledgeSupported {
                PhoneSectionHeader(title: "KNOWLEDGE")
                    .profileRow()
                NavigationLink {
                    KnowledgeView(client: client)
                } label: {
                    row(label: "knowledge", value: "project notes")
                }
                .profileRow()
            }

            if client.snapshot.board.accessLogSupported {
                PhoneSectionHeader(title: "SECURITY")
                    .profileRow()
                NavigationLink {
                    AccessLogView(client: client, timing: client.linkTiming)
                } label: {
                    row(label: "access log", value: "refused knocks and link timing")
                }
                .profileRow()
            }

            forgetButton
                .profileRow()
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Theme.bg)
        .navigationTitle("profile")
        .decryptSurface("ProfileView")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar(.visible, for: .navigationBar)
        .toolbarBackground(Theme.bar, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        .confirmationDialog("Forget this Mac?",
                            isPresented: $confirmingForget,
                            titleVisibility: .visible) {
            DecryptButton("Forget this Mac", role: .destructive, action: onForget)
            DecryptButton("Keep it", role: .cancel) {}
        } message: {
            Text("The phone forgets its key; pair again from the Mac's Devices menu.")
        }
    }

    /// **BOT ACCESS** — the Mac's bot, its Read and its Write, each off, on
    /// for 1, 6 or 24 hours, or on with no timer; the same switches as the
    /// bot's entry under Devices on the Mac. Drawn only where the Mac
    /// publishes `bot_access` on a row — an older Mac publishes none and the
    /// section is absent. The words are `BotAccessRules`'; the Mac re-checks
    /// every request the bot sends against its own copy.
    @ViewBuilder
    private var botSection: some View {
        if let bot = client.snapshot.devices.bot, let access = bot.botAccess {
            PhoneSectionHeader(title: "BOT ACCESS")
                .profileRow()
            row(label: "device", value: bot.name.isEmpty ? bot.id : bot.name)
                .profileRow()
            botSide("read", botId: bot.id, grant: access.read,
                    selection: $botReadMode)
            botSide("write", botId: bot.id, grant: access.write,
                    selection: $botWriteMode)
        }
    }

    /// One side of the bot's access: the position in words, a menu of the
    /// five, RESTART TIMER while a timer runs, and the Mac's refusal inline.
    @ViewBuilder
    private func botSide(_ side: String, botId: String, grant: PhoneBotGrant,
                         selection: Binding<String>) -> some View {
        row(label: side,
            value: botSending == side ? "SENDING…"
                : BotAccessRules.words(mode: grant.mode, until: grant.until,
                                       now: Date().timeIntervalSince1970))
            .profileRow()
        Picker(selection: selection.decrypting(decryptFeedback)) {
            ForEach(BotAccessRules.modes, id: \.self) { mode in
                Text(BotAccessRules.title(mode)).tag(mode)
            }
        } label: {
            Text("\(side) access")
                .font(Theme.mono(12))
                .foregroundStyle(Theme.faint)
        }
        .font(Theme.mono(12))
        .tint(Theme.phosphor)
        .disabled(!botSending.isEmpty)
        .accessibilityLabel("The bot's \(side) access")
        .padding(.horizontal, 14)
        .padding(.vertical, 6)
        .profileRow()
        .onAppear { reseedBot(side, selection, to: grant.mode) }
        .onChange(of: grant.mode) { _, published in
            reseedBot(side, selection, to: published)
        }
        .onChange(of: selection.wrappedValue) { _, chosen in
            // A change this screen made itself never posts: a re-seed from
            // the Mac, or the menu put back after a refusal.
            if botReseeding.remove(side) != nil { return }
            let published = publishedBotMode(side)
            guard BotAccessRules.shouldPost(chosen: chosen,
                                            published: published) else { return }
            sendBotAccess(botId: botId, side: side, mode: chosen,
                          selection: selection)
        }
        if let refusal = botRefusal[side], !refusal.isEmpty {
            row(label: "refused", value: refusal, faint: true)
                .profileRow()
        }
        if BotAccessRules.showsRestart(grant.mode) {
            DecryptButton(botSending == side ? "SENDING…" : "RESTART TIMER") {
                sendBotAccess(botId: botId, side: side, mode: grant.mode,
                              selection: selection)
            }
            .buttonStyle(AlarmOutline())
            .disabled(!botSending.isEmpty)
            .accessibilityLabel("Restart the \(side) timer")
            .accessibilityHint("Starts the \(side) timer again from now")
            .font(Theme.mono(12))
            .padding(.horizontal, 14)
            .padding(.vertical, 6)
            .profileRow()
        }
    }

    /// The position the Mac publishes for one side right now, read at the
    /// moment of use — never a value captured before an await.
    private func publishedBotMode(_ side: String) -> String {
        guard let access = client.snapshot.devices.bot?.botAccess else { return "" }
        return side == "write" ? access.write.mode : access.read.mode
    }

    /// Move one side's menu without it counting as a press. Marked only when
    /// the value actually changes, since an unchanged value fires no
    /// `onChange` and a mark left behind would swallow the next real press.
    private func reseedBot(_ side: String, _ selection: Binding<String>,
                           to mode: String) {
        guard selection.wrappedValue != mode else { return }
        botReseeding.insert(side)
        selection.wrappedValue = mode
    }

    /// Post one position. A failed press is never re-sent later behind the
    /// person's back (`ReceiptEffect.botAccess`, which the sweep drops
    /// before any resend), so SENDING… is shown
    /// while it is out, a refusal is shown inline in the Mac's own words,
    /// and the menu goes back to whatever the Mac publishes *now*.
    private func sendBotAccess(botId: String, side: String, mode: String,
                               selection: Binding<String>) {
        guard botSending.isEmpty else { return }
        botSending = side
        botRefusal[side] = nil
        Task {
            let result = await client.post(
                action: PhoneActions.setBotAccess,
                fields: ["device_id": botId, "side": side, "mode": mode])
            botSending = ""
            if !result.ok {
                botRefusal[side] = BotAccessRules.refusalWords(result.detail)
                reseedBot(side, selection, to: publishedBotMode(side))
            }
        }
    }

    /// **PREPARE ON THIS PHONE** — the person's own Anthropic key, so the
    /// new-card screen's PREPARE keeps working with the Mac out of reach.
    /// The key itself is never on screen: with one saved the row reads
    /// "key saved · ends …1234" plus REPLACE and REMOVE; without, a secure
    /// field and SAVE.
    @ViewBuilder
    private var prepareSection: some View {
        PhoneSectionHeader(title: "PREPARE ON THIS PHONE")
            .profileRow()
        if !keySuffix.isEmpty && !replacingKey {
            row(label: "anthropic key", value: "key saved · ends …\(keySuffix)")
                .profileRow()
            HStack(spacing: 12) {
                DecryptButton("REPLACE") { replacingKey = true }
                    .buttonStyle(AlarmOutline())
                DecryptButton("REMOVE") { confirmingKeyRemoval = true }
                    .buttonStyle(AlarmOutline(color: Theme.alarm))
                    .accessibilityHint("Asks first")
            }
            .font(Theme.mono(12))
            .padding(.horizontal, 14)
            .padding(.vertical, 6)
            .profileRow()
            .confirmationDialog("Remove the key from this phone?",
                                isPresented: $confirmingKeyRemoval,
                                titleVisibility: .visible) {
                DecryptButton("Remove key", role: .destructive) {
                    AnthropicKeyStore.remove()
                    keySuffix = ""
                }
                DecryptButton("Keep it", role: .cancel) {}
            } message: {
                Text("Prepare will then need the Mac again.")
            }
        } else {
            SecureField("paste an Anthropic API key", text: $keyDraft)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.phosphor)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
                .hidesKeyboard()
                .padding(.horizontal, 14)
                .padding(.vertical, 6)
                .profileRow()
            HStack(spacing: 12) {
                DecryptButton("SAVE") {
                    if AnthropicKeyStore.save(keyDraft) {
                        keyDraft = ""
                        keySuffix = AnthropicKeyStore.suffix
                        replacingKey = false
                    }
                }
                .disabled(keyDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .buttonStyle(AlarmOutline())
                if replacingKey {
                    DecryptButton("CANCEL") {
                        keyDraft = ""
                        replacingKey = false
                    }
                    .buttonStyle(AlarmOutline())
                }
            }
            .font(Theme.mono(12))
            .padding(.horizontal, 14)
            .padding(.vertical, 6)
            .profileRow()
        }
        row(label: "note", value: "Used only when the Mac is out of reach, sent only to Anthropic.",
            faint: true)
            .profileRow()
    }

    /// **QUEUE** — every press in play or stuck, **oldest first** so the
    /// order the presses will go in can be read, plus a refusal the person
    /// read while it is fresh (`ReceiptLedger.queueRows`).
    ///
    /// Absent at zero, `RecentlyView`'s rule: a section that is always there
    /// saying nothing is noise. A `stuck` press **stays listed** — that is
    /// the whole point of the state, and the thing this section exists to
    /// stop being a control that quietly stopped looking busy.
    ///
    /// Each row leads with its status word — QUEUED, SENDING…, SENT, LANDING,
    /// REFUSED, STUCK; the word is the signal, the amber on a refusal never
    /// the only one — then says the Mac's own words wherever it has spoken,
    /// and offers exactly two presses: RETRY, which resets the backoff and
    /// mints a fresh one-time mark (a no-op on a press still queued, which
    /// will go in turn anyway), and DISCARD, which forgets the record and
    /// sends nothing. Neither shows anything of the pairing itself.
    @ViewBuilder
    private var pendingSection: some View {
        let rows = receipts.queueRows
        if !rows.isEmpty {
            PhoneSectionHeader(title: "QUEUE (\(rows.count))")
                .accessibilityLabel("PENDING (\(rows.count)) — the queue, oldest first")
                .profileRow()
            ForEach(rows) { receipt in
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 6) {
                        Text(receipt.statusWord)
                            .font(Theme.mono(9, weight: .semibold))
                            .tracking(0.8)
                            .foregroundStyle(
                                receipt.statusWord == "REFUSED"
                                    || receipt.statusWord == "STUCK"
                                    ? Theme.amber : Theme.faint)
                        Text(receipt.wording)
                            .font(Theme.mono(12))
                            .foregroundStyle(Theme.phosphor)
                        if !receipt.subject.isEmpty {
                            Text(receipt.subject)
                                .font(Theme.mono(11))
                                .foregroundStyle(Theme.faint)
                        }
                    }
                    if !receipt.line.isEmpty {
                        Text(receipt.line)
                            .font(Theme.mono(11))
                            .foregroundStyle(receipt.state == .stuck
                                             ? Theme.amber : Theme.dim)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    HStack(spacing: 12) {
                        DecryptButton(retryingReceipt == receipt.id
                               ? "SENDING…" : "RETRY") {
                            let id = receipt.id
                            // A queued press needs no RETRY: it has not
                            // gone yet, and a fresh one-time mark here
                            // would be a second copy of it. A press still
                            // going out keeps its mark too: re-minting it
                            // mid-transmit would make the in-flight answer
                            // a no-op and send the press twice.
                            guard receipt.state != .queued,
                                  receipt.state != .sending else { return }
                            Task {
                                retryingReceipt = id
                                defer { retryingReceipt = "" }
                                await client.retryReceipt(id)
                            }
                        }
                        .buttonStyle(AlarmOutline())
                        // Every RETRY, not just the pressed one:
                        // `flushReceipts` is a serialized sweep, so a second
                        // press cannot start a second send.
                        .disabled(!retryingReceipt.isEmpty)
                        .accessibilityLabel(
                            retryingReceipt == receipt.id
                            ? "Sending \(receipt.wording) again"
                            : "Send \(receipt.wording) again")
                        DecryptButton("DISCARD") {
                            receipts.remove(receipt.id)
                        }
                        .buttonStyle(AlarmOutline(color: Theme.alarm))
                        // Off while a RETRY is out, and while **this** press
                        // is going out: forgetting a record mid-transmit
                        // loses the answer to a press that may still land.
                        .disabled(!retryingReceipt.isEmpty
                                  || receipt.state == .sending)
                        // Names its receipt: a list of pending presses is a
                        // list of identical DISCARD buttons otherwise.
                        .accessibilityLabel("Throw away \(receipt.wording)")
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 6)
                .profileRow()
            }
        }
    }

    private func row(label: String, value: String, faint: Bool = false) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 8)
            Text(value)
                .font(Theme.mono(12))
                .foregroundStyle(faint ? Theme.dim : Theme.phosphor)
                .multilineTextAlignment(.trailing)
                .fixedSize(horizontal: false, vertical: true)
                .monospacedDigit()
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 6)
    }

    /// "last heard Ns ago", ticking on the phone's own clock. Only this text
    /// sits inside the `TimelineView`, so the tick redraws nothing else —
    /// `StaleBanner`'s rule, the one clock the design system allows.
    private func lastHeardRow(_ heard: Date) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text("last heard")
                .font(Theme.mono(12))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 8)
            TimelineView(.periodic(from: .now, by: 1)) { context in
                Text("\(max(0, Int(context.date.timeIntervalSince(heard))))s ago")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.phosphor)
                    .monospacedDigit()
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 6)
        // The label and its ticking figure are one reading, not two.
        .accessibilityElement(children: .combine)
    }

    private var forgetButton: some View {
        DecryptButton("FORGET THIS MAC") {
            confirmingForget = true
        }
        .accessibilityHint("Asks first; the phone then has to be paired again")
        .buttonStyle(AlarmOutline(color: Theme.alarm))
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 14)
        .padding(.top, 24)
        .padding(.bottom, 14)
    }
}

private extension View {
    /// `UsageView`'s row dressing: no insets, no separator, the theme's own
    /// background.
    func profileRow() -> some View {
        self
            .listRowInsets(EdgeInsets())
            .listRowSeparator(.hidden)
            .listRowBackground(Theme.bg)
    }
}

/// The WIDGET rows' figures, on the phone's own clock. Reads the App Group
/// note and `UserDefaults`; no network.
@MainActor
enum WidgetDiagnostics {
    static func rows(client: PhoneClient, now: Date = Date()) -> [(String, String)] {
        let defaults = UserDefaults.standard
        let note: String
        if FleetSummary.fileURL() == nil {
            note = "no shared container"
        } else if let summary = FleetSummary.load() {
            note = "\(age(summary.generatedAt, now: now)) · \(summary.needsYou)/\(summary.working)/\(summary.todo)"
        } else {
            note = "none"
        }
        let write = client.widgetWriteReport.isEmpty
            ? "not yet this launch" : client.widgetWriteReport
        let askedAt = defaults.double(forKey: WidgetReloadBudget.lastReloadAtKey)
        let asked = askedAt > 0 ? age(askedAt, now: now) : "never"
        let stamps = defaults.array(forKey: WidgetReloadBudget.stampsKey)
            as? [Double] ?? []
        let spent = WidgetReloadBudget.recent(stamps, now: now).count
        return [
            ("note on disk", note),
            ("last write", write),
            ("last reload asked", asked),
            ("counted reloads (2h)", "\(spent) of \(WidgetReloadBudget.perWindow)"),
        ]
    }

    /// "42s ago", "7m ago", "3h ago", "1d 4h ago".
    static func age(_ epoch: Double, now: Date) -> String {
        let s = max(0, Int(now.timeIntervalSince1970 - epoch))
        if s < 60 { return "\(s)s ago" }
        if s < 3600 { return "\(s / 60)m ago" }
        if s < 86400 { return "\(s / 3600)h ago" }
        return "\(s / 86400)d \((s % 86400) / 3600)h ago"
    }
}
