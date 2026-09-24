import SwiftUI

/// The approval strip: what the session wants to run, and the two answers.
///
/// Drawn in the stdout pane. No arm-then-confirm, unlike
/// Stop and Retire. Those are irreversible acts Dark Army initiates; this is a
/// question already on screen in the session's own terminal where one
/// keypress answers it. The command itself is shown instead, because that
/// is what makes one press safe. `inputPreview` is capped at three lines
/// so the reserved 110pt strip can still fit the buttons.
struct PermissionBar: View {
    let agent: Agent
    let prompt: PermissionPrompt
    @ObservedObject var actions: RowActions
    @ObservedObject var client: DaemonClient

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 5) {
                Image(systemName: "hand.raised.fill")
                    .font(.system(size: 9))
                Text(prompt.summary)
                    .font(.system(size: 11.5, weight: .medium))
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .foregroundStyle(.orange)

            // Buttons above the command so nickname + summary + Allow/Deny
            // stay above the 110pt fold; only the preview may scroll.
            if prompt.answerable {
            HStack(spacing: 8) {
                Button { answer(allow: true) } label: {
                    Label("Allow", systemImage: "checkmark")
                }
                Button { answer(allow: false) } label: {
                    Label("Deny", systemImage: "xmark")
                }
                Spacer(minLength: 0)
                Text("or answer in the terminal")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            } else {
                Text(prompt.answerNote)
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
            }

            if !prompt.inputPreview.isEmpty {
                ScrollView {
                    Text(prompt.inputPreview)
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(3)
                        .fixedSize(horizontal: false, vertical: true)
                        .selectable(false)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 42)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(Color.orange.opacity(0.10))
    }

    private func answer(allow: Bool) {
        actions.answerPermission(prompt, allow: allow, agent: agent, client: client)
    }
}

/// Answering an idle session without leaving the panel.
///
/// Shared by the old row and the attention strip. Drawn only when the
/// snapshot says Dark Army can reach this session. A button only where the
/// agent named a choice; Send stays disabled on an empty field. Hidden
/// by the caller when a prompt is up or the row is not stopped.
struct ReplyBar: View {
    let agent: Agent
    @ObservedObject var actions: RowActions
    @ObservedObject var client: DaemonClient
    var onFocusChange: (Bool) -> Void = { _ in }

    @State private var replyText = ""
    @FocusState private var fieldFocused: Bool

    /// The route the daemon says the next reply takes, in the placeholder
    /// and the Send button's help — so nobody guesses whether the channel
    /// was needed. Read verbatim; anything but `"typed"` reads as the channel.
    static func placeholder(replyVia: String) -> String {
        replyVia == "typed" ? "Reply — typed into its terminal…" : "Reply…"
    }

    static func sendHelp(replyVia: String) -> String {
        replyVia == "typed"
            ? "Typed onto this session's own input line in VS Code"
            : "Sent through Dark Army's channel"
    }

    var body: some View {
        let typed = replyText.trimmingCharacters(in: .whitespacesAndNewlines)
        let sending = actions.isSending(agent)
        return HStack(spacing: 6) {
            TextField(Self.placeholder(replyVia: agent.replyVia),
                      text: $replyText, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(1...4)
                .font(.system(size: 11.5))
                .focused($fieldFocused)
                .onSubmit(send)
                .disabled(sending)
            if DictateButton.available(client.context.settings) {
                DictateButton(focus: { fieldFocused = true })
                    .controlSize(.small)
            }
            if sending {
                // `.caret`, not `.line`: a sentence here would squeeze the
                // field a person is typing into.
                AgentChatterView(.caret, wait: .sending, seed: agent.id,
                                 spoken: "Sending your reply")
                    .id(agent.id)
            } else if typed.isEmpty, !agent.replyOptions.isEmpty {
                ForEach(agent.replyOptions, id: \.self) { option in
                    Button(option) { send(option) }
                        .clickable()
                }
            } else {
                Button("Send", action: send)
                    .disabled(typed.isEmpty)
                    .help(Self.sendHelp(replyVia: agent.replyVia))
            }
        }
        .controlSize(.small)
        .padding(.horizontal, 14)
        .padding(.bottom, 8)
        .onChange(of: fieldFocused) { _, focused in
            onFocusChange(focused)
        }
    }

    private func send() {
        actions.reply(agent, text: replyText, client: client)
        replyText = ""
    }

    private func send(_ option: String) {
        actions.reply(agent, text: option, client: client)
        replyText = ""
    }
}

/// One option of an `AskUserQuestion`, shared by the old row and the attention
/// strip for `ReplyBar`'s reason: two copies of one control drift.
///
/// A button only where `canType` says the daemon can type the answer into the
/// session's terminal; elsewhere the same words as plain secondary text,
/// because the label is worth reading even where it cannot be pressed. The
/// number is the same digit the terminal's dialog binds, so a reader can check
/// the two surfaces against each other. No arm-then-confirm — see
/// `RowActions.answerQuestion`. The label wraps to two lines rather than
/// truncating: it is a choice now, and a choice that ends in an ellipsis is
/// one nobody can make.
struct QuestionOptionRow: View {
    let agent: Agent
    let index: Int
    let label: String
    var detail: String = ""
    @ObservedObject var actions: RowActions
    @ObservedObject var client: DaemonClient

    var body: some View {
        if agent.canType {
            Button {
                actions.answerQuestion(agent, index: index, client: client)
            } label: {
                content(numberStyle: .secondary, labelStyle: .primary)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .clickable()
            .disabled(actions.isSending(agent))
        } else {
            content(numberStyle: .tertiary, labelStyle: .secondary)
        }
    }

    private func content(numberStyle: some ShapeStyle,
                         labelStyle: some ShapeStyle) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            HStack(alignment: .firstTextBaseline, spacing: 5) {
                Text("\(index + 1).")
                    .font(.system(size: 10.5).monospacedDigit())
                    .foregroundStyle(numberStyle)
                Text(label)
                    .font(.system(size: 10.5))
                    .foregroundStyle(labelStyle)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !detail.isEmpty {
                Text(detail)
                    .font(.system(size: 10))
                    .foregroundStyle(Theme.faint)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, 18)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// The end of reading: acknowledge the turn by closing the terminal tab.
/// Shared by the old row and the attention tile.
///
/// **Its own strip, under everything else, behind a hairline.** It was tempting
/// to hang it off the end of the reply bar — one more button beside Send, no
/// extra height — and that is wrong twice. It is not a reply: nothing is said to
/// the agent, the tab and the process both go, and a control that destroys
/// something must not sit a few points from the one that sends "ok". And the two
/// have *different reach* — the reply bar needs Dark Army's channel, this needs a VS
/// Code window that can dispose the tab — so hanging one off the other
/// would hide it on exactly the sessions where it still works.
///
/// The label states the consequence before the press and the risk after it, so
/// the armed state is never colour-alone.
struct WrapUpBar: View {
    static let prompt = "Done reading? Close this terminal tab and end the session."
    static let armedPrompt = "Closes the tab and ends this agent. There is no undo."
    static let button = "Acknowledge & close terminal"
    static let armedButton = "Really close the terminal?"

    let agent: Agent
    @ObservedObject var actions: RowActions
    @ObservedObject var client: DaemonClient
    /// Horizontal margin. 14 on the old row, matching `ReplyBar`; 0 on the
    /// attention tile, which already carries its own.
    var inset: CGFloat = 14

    var body: some View {
        let armed = actions.armedWrapUp == agent.id
        let busy = actions.isWrappingUp(agent)
        return VStack(spacing: 0) {
            Divider().opacity(0.6)
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(armed ? Self.armedPrompt : Self.prompt)
                    .font(.system(size: 10.5))
                    .foregroundStyle(armed ? Color.red : Color.secondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                if busy {
                    AgentChatterView(.caret, wait: .closing, seed: agent.id,
                                     spoken: "Closing the terminal")
                        .id(agent.id)
                }
                Button { actions.wrapUp(agent, client: client) } label: {
                    Label(armed ? Self.armedButton : Self.button,
                          systemImage: armed ? "exclamationmark.triangle" : "xmark.circle")
                }
                .controlSize(.small)
                .tint(armed ? Color.red : nil)
                .disabled(busy)
                .help("Closes this session's terminal tab — the agent ends with it — and dismisses its card. Press twice to confirm.")
                .accessibilityLabel(armed ? "Confirm close terminal" : "Acknowledge and close terminal")
            }
            .padding(.top, 8)
        }
        .buttonStyle(.bordered)
        .padding(.horizontal, inset)
        .padding(.top, 8)
        .padding(.bottom, 8)
    }
}

/// `WrapUpBar`'s twin for a rate-limited Claude session: types
/// `/low-priority` onto the session's input line so it carries on in Claude
/// Code's low-priority mode. Drawn only where `agent.canLowPriority` says the
/// daemon would accept the press. The command is a toggle, which is why the
/// armed copy says so and the button is armed-then-confirmed.
struct LowPriorityBar: View {
    static let prompt = "This session hit its usage limit. Carry on in low priority?"
    static let armedPrompt = "Types the low-priority command into this terminal. Running it twice switches it back off."
    static let button = "Low priority"
    static let armedButton = "Really switch to low priority?"

    let agent: Agent
    @ObservedObject var actions: RowActions
    @ObservedObject var client: DaemonClient
    var inset: CGFloat = 14

    var body: some View {
        let armed = actions.armedLowPriority == agent.id
        return VStack(spacing: 0) {
            Divider().opacity(0.6)
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(armed ? Self.armedPrompt : Self.prompt)
                    .font(.system(size: 10.5))
                    .foregroundStyle(armed ? Color.red : Color.secondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                Button { actions.lowPriority(agent, client: client) } label: {
                    Label(armed ? Self.armedButton : Self.button,
                          systemImage: armed ? "exclamationmark.triangle" : "tortoise")
                }
                .controlSize(.small)
                .tint(armed ? Color.red : nil)
                .help("Types /low-priority into this session's terminal and dismisses its card. The command is a toggle — running it twice switches low priority back off. Press twice to confirm.")
                .accessibilityLabel(armed ? "Confirm switch to low priority" : "Switch to low priority")
            }
            .padding(.top, 8)
        }
        .buttonStyle(.bordered)
        .padding(.horizontal, inset)
        .padding(.top, 8)
        .padding(.bottom, 8)
    }
}

enum Format {
    static func duration(_ seconds: Double) -> String {
        let s = Int(max(0, seconds))
        if s < 60 { return "\(s)s" }
        if s < 3600 { return "\(s / 60)m" }
        return "\(s / 3600)h \(String(format: "%02d", (s % 3600) / 60))m"
    }

    static func usd(_ value: Double) -> String {
        if value > 0 && value < 0.01 { return "<$0.01" }
        return String(format: "$%.2f", value)
    }

    static func count(_ value: Int) -> String {
        if value >= 1_000_000 { return String(format: "%.1fM", Double(value) / 1e6) }
        if value >= 1_000 { return String(format: "%.1fk", Double(value) / 1e3) }
        return "\(value)"
    }

    /// Parse the model id rather than look it up, so a newly-shipped model reads
    /// correctly without a code change — the same rule the menu bar follows.
    static func model(_ id: String?) -> String {
        guard var text = id, !text.isEmpty else { return "—" }
        if text.lowercased().hasPrefix("grok") {
            // grok-4.6 / grok-4.6-build → "Grok 4.6". Same parse-not-lookup
            // rule as the Claude families: a newly shipped id still reads.
            let rest = text.split(separator: "-").dropFirst()
            let version = rest.first.map(String.init) ?? ""
            return version.isEmpty ? "Grok" : "Grok \(version)"
        }
        let million = text.contains("[1m]")
        text = text.replacingOccurrences(of: "[1m]", with: "")
        let parts = text.split(separator: "-")
        var family = ""
        var version = ""
        for (i, part) in parts.enumerated() {
            if ["opus", "sonnet", "haiku", "fable"].contains(part.lowercased()) {
                family = part.capitalized
                if i + 1 < parts.count, Int(parts[i + 1]) != nil {
                    version = String(parts[i + 1])
                }
            }
        }
        if family.isEmpty { return text }
        return "\(family) \(version)\(million ? " (1M)" : "")"
            .trimmingCharacters(in: .whitespaces)
    }
}

/// Which harness an agent is running under.
///
/// The same two marks the menu-bar strip puts beside its budget percentages — a
/// radiating burst for Claude, a slanted X for Grok — so a reader who learned
/// them up there gets this for free. Shape rather than colour, like everything
/// else here that has to mean something.
///
/// Drawn rather than typed because there is no font that has both, and because a
/// letter ("C", "G") makes the reader decode before they can read.
/// A provider's mark, from the real artwork.
///
/// These were hand-drawn silhouettes until the actual logos were supplied, on
/// the reasoning — still sound, and still written out in `app.py`'s
/// `_draw_brand_mark` — that a nine-point mark is a silhouette and its only job
/// is to say which of the two accounts a number belongs to. What that reasoning
/// could not fix was being *wrong*: Grok's mark is a ring cut by a diagonal
/// blade, and the drawn stand-in was a slanted X, which is not a simplification
/// of that shape but a different one. Claude's burst was closer — the real mark
/// has twelve rays of uneven length rather than eight even ones.
///
/// **Template images, not colour art.** The PNGs are reduced to their alpha
/// channel at bake time, so AppKit paints them in whatever ink is current. That
/// is not a stylistic preference: Grok's mark is black, and this panel is
/// routinely dark, so shipping the original colours would put a black mark on a
/// dark ground — the same trap `_draw_brand_mark`, `_usage_image` and
/// `ICON_DISCONNECTED` were each caught by, which is why everything drawn here
/// takes its ink from the context rather than carrying its own.
struct ProviderMark: View {
    let provider: String
    var size: CGFloat = 9
    /// Ink. Secondary everywhere the mark is furniture beside a number; the
    /// caller passes its own when the mark sits inside something already
    /// coloured, where grey would read as a different kind of thing.
    var tint: Color = .secondary

    var body: some View {
        Group {
            if let image = Self.image(for: provider) {
                Image(nsImage: image)
                    .resizable()
                    .renderingMode(.template)
                    .interpolation(.high)
                    .aspectRatio(contentMode: .fit)
            } else {
                // Artwork missing from the bundle is a build problem, not a
                // reason to shift the layout: hold the slot and draw nothing.
                Color.clear
            }
        }
        .frame(width: size, height: size)
        .foregroundStyle(tint)
        .accessibilityLabel(Self.displayName(provider))
    }

    /// Loaded once per provider. `NSImage(contentsOf:)` hits the disk, and these
    /// marks are drawn on every agent row and every usage chip, on every push.
    private static var cache: [String: NSImage] = [:]

    /// Spoken and on-screen name. Same default as `image(for:)` — empty and
    /// unknown wear Claude's mark, so they also read "Claude".
    static func displayName(_ provider: String) -> String {
        switch provider {
        case "grok": return "Grok"
        case "codex": return "Codex"
        default: return "Claude"
        }
    }

    private static func image(for provider: String) -> NSImage? {
        let name: String
        switch provider {
        case "grok": name = "grok"
        case "codex": name = "codex"
        default: name = "claude"
        }
        if let hit = cache[name] { return hit }
        let ext = provider == "codex" ? "svg" : "png"
        guard let url = PanelResources.url(folder: "brand", file: "\(name)-mark.\(ext)"),
              let image = NSImage(contentsOf: url)
        else { return nil }
        image.isTemplate = true
        cache[name] = image
        return image
    }
}

private extension View {
    /// Text selection, but only where the text is fully laid out.
    ///
    /// A selectable `Text` that is *truncated* misbehaves twice over: it eats
    /// the tap its row is listening for, and a click can lay the whole string
    /// out past its own `lineLimit` — into a height the enclosing `LazyVStack`
    /// measured before the click and does not measure again, so the overflow
    /// paints across the rows below. Both were measured by clicking, not
    /// reasoned about.
    @ViewBuilder func selectable(_ on: Bool) -> some View {
        if on { textSelection(.enabled) } else { textSelection(.disabled) }
    }
}
