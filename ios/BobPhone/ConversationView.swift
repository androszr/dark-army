import ImageIO
import SwiftUI
import UIKit

struct ConversationScreen<Header: View>: View {
    let agent: Agent
    let stopped: Bool
    @ObservedObject var client: PhoneClient
    var retainedReply: PhoneReplyDraft? = nil
    /// The keyboard is up over this screen: the page scrolls to the answer
    /// box, so the field and SEND sit above the keyboard.
    var typing: Bool = false
    /// The waiting agent's question, in words, drawn right above the answer
    /// controls: an `AskUserQuestion`
    /// call's tool line carries no words of its own. Empty for no question.
    var ask: String = ""
    /// Drawn above the turns, inside the one page. Empty at every call
    /// site today.
    private let header: Header
    @State private var expanded: Set<Int>
    /// The folded tool runs that are open, keyed by the run's first `seq`,
    /// so a run that grows keeps its state.
    @State private var openRuns: Set<Int>
    @State private var atBottom: Bool
    @State private var onScreen: Bool
    /// How many of the newest turns are drawn. The cache holds the whole
    /// conversation; the list draws a window of it, widened by
    /// `windowStep` at a time from the top.
    @State private var window: Int
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @EnvironmentObject private var sheets: PhoneSheetRouter
    // Computed, not stored: a generic type cannot hold a static stored
    // property, and the archive build refuses it.
    static var windowStep: Int { 300 }

    init(agent: Agent, stopped: Bool, client: PhoneClient,
         retainedReply: PhoneReplyDraft? = nil,
         ask: String = "",
         typing: Bool = false,
         @ViewBuilder header: () -> Header) {
        self.agent = agent
        self.stopped = stopped
        self.client = client
        self.retainedReply = retainedReply
        self.ask = ask
        self.typing = typing
        self.header = header()
        self._expanded = State(initialValue: [])
        self._openRuns = State(initialValue: [])
        self._atBottom = State(initialValue: true)
        self._onScreen = State(initialValue: false)
        self._window = State(initialValue: Self.windowStep)
    }

    private var turns: [ConversationTurn] {
        client.conversationCache.turns(agent.sessionId)
    }

    private var drawn: ArraySlice<ConversationTurn> {
        turns.suffix(window)
    }

    /// The drawn window as rows: no result rows, runs of tool calls folded
    /// (`ConversationFold`). Every scroll anchor reads these ids, never
    /// `turns.last`, which may be a result that has no row.
    private var rows: [ConversationFold.Row] {
        ConversationFold.rows(Array(drawn))
    }

    private var nickname: String {
        agent.nickname.isEmpty ? String(agent.sessionId.prefix(8)) : agent.nickname
    }

    private var catchingUp: Bool {
        client.conversationWatching == agent.sessionId
            && client.conversationCache.nextSeq(agent.sessionId)
            < (client.conversationCache.conversations[agent.sessionId]?.total ?? 0)
    }

    /// The answer box's scroll id: typing and a new turn scroll here, so
    /// the field, SEND and the latest line are what is in view.
    static var answerAnchor: String { "conversation.answer" }

    /// One scrolling page — the status lines, the header, the turns, then
    /// the ask and its answer buttons — over a message composer pinned
    /// beneath it (26 Sep 2026). The composer is a sibling in the stack, not
    /// an overlay, so it shortens the page rather than painting over it, and
    /// it is drawn whether the agent is working or stopped: a person can
    /// always say something to the agent they are reading. A long ask or an
    /// interview's options still simply scroll. Opens at the foot.
    var body: some View {
        VStack(spacing: 0) {
            page
            Rectangle().fill(Theme.hair).frame(height: 1)
            AnswerBox(agent: agent, stopped: stopped, client: client,
                      retainedReply: retainedReply, part: .composer)
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(Theme.bg)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear {
            onScreen = true
            client.watchConversation(agent.sessionId)
            Task { await client.catchUpConversation(agent.sessionId) }
        }
        .onDisappear {
            // Only this screen's own watch: a sheet closing over another
            // agent's screen must not clear the one underneath, whose
            // `onAppear` may already have run.
            onScreen = false
            client.watchConversation(nil, ifWatching: agent.sessionId)
        }
        .onChange(of: client.conversationWatching) { _, watching in
            // Back in front with the watch gone: take it again.
            guard onScreen, watching == nil else { return }
            client.watchConversation(agent.sessionId)
        }
        .onChange(of: agent.sessionId) { _, sid in
            window = Self.windowStep
            client.watchConversation(sid)
            Task { await client.catchUpConversation(sid) }
        }
    }

    private var page: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    VStack(alignment: .leading, spacing: 0) { statusLines }
                    header
                        .frame(maxWidth: .infinity, alignment: .top)
                    LazyVStack(alignment: .leading, spacing: 10) {
                        if turns.count > window {
                            earlierControl
                        }
                        ForEach(rows) { row in
                            rowView(row)
                                .id(row.id)
                        }
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    Rectangle().fill(Theme.hair).frame(height: 1)
                    VStack(alignment: .leading, spacing: 8) {
                        if !ask.isEmpty {
                            Text(ask)
                                .font(Theme.mono(13))
                                .foregroundStyle(Theme.phosphorBright)
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        AnswerBox(agent: agent, stopped: stopped, client: client,
                                  part: .choices)
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .id(Self.answerAnchor)
                    .onAppear { atBottom = true }
                    .onDisappear { atBottom = false }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            // A drag down the page puts the keyboard away.
            .scrollDismissesKeyboard(.interactively)
            .defaultScrollAnchor(.bottom)
            .onAppear { proxy.scrollTo(Self.answerAnchor, anchor: .bottom) }
            .onChange(of: turns.count) { _, _ in
                guard atBottom else { return }
                proxy.scrollTo(Self.answerAnchor, anchor: .bottom)
            }
            .onChange(of: typing) { _, up in
                // The keyboard came up over the page: bring the field and
                // SEND above it.
                guard up else { return }
                Motion.animate(.easeOut(duration: 0.2), reduced: reduceMotion) {
                    proxy.scrollTo(Self.answerAnchor, anchor: .bottom)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    /// One row: a turn as it always was, or a folded run of tool calls.
    @ViewBuilder private func rowView(_ row: ConversationFold.Row) -> some View {
        switch row {
        case .turn(let turn):
            ConversationTurnRow(
                turn: turn,
                nickname: nickname,
                expanded: expanded.contains(turn.seq),
                result: result(for: turn),
                onToggle: { toggle(turn.seq) },
                onImage: { path in sheets.show(.image(agent.sessionId, path)) }
            )
        case .run(let tools):
            ConversationRunRow(
                tools: tools,
                open: ConversationFold.runIsOpen(tools, id: row.id,
                                                 openRuns: openRuns,
                                                 expanded: expanded),
                nickname: nickname,
                expanded: expanded,
                result: { result(for: $0) },
                onToggleRun: { toggleRun(row.id, tools: tools) },
                onToggleTool: { toggle($0) }
            )
        }
    }

    /// The turns above the drawn window, one press for another
    /// `windowStep` of them.
    private var earlierControl: some View {
        let hidden = turns.count - window
        return DecryptButton(action: { window += Self.windowStep }) {
            Text("// earlier… \(hidden) more turns")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Show \(min(hidden, Self.windowStep)) earlier turns")
    }

    @ViewBuilder private var statusLines: some View {
        if catchingUp {
            HStack(spacing: 8) {
                Text(catchingLine)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                AgentChatterView(.caret, wait: .opening, seed: agent.sessionId,
                                 spoken: "Catching up on the conversation")
                    .id(agent.sessionId)
            }
            .padding(.horizontal, 14)
            .padding(.top, 6)
        }
        if let first = turns.first, first.seq > 0 {
            Text("// earlier turns are not kept on this phone")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 14)
                .padding(.top, 6)
        }
        if !client.snapshot.board.conversationSupported {
            Text("// older Mac — update Dark Army to read conversations here")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 14)
                .padding(.top, 6)
        }
        if let reason = client.conversationUnavailable[agent.sessionId],
           !reason.isEmpty, turns.isEmpty {
            Text("// no conversation yet")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 14)
                .padding(.top, 4)
            Text(reason)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 14)
        }
    }

    private var catchingLine: String {
        let held = client.conversationCache.conversations[agent.sessionId]
        let next = held?.nextSeq ?? 0
        let total = held?.total ?? 0
        return "// catching up · \(next) of \(total) turns"
    }

    private func result(for turn: ConversationTurn) -> ConversationTurn? {
        guard turn.kind == "tool", expanded.contains(turn.seq),
              !turn.callId.isEmpty else { return nil }
        return turns.first { $0.kind == "result" && $0.callId == turn.callId }
    }

    private func toggle(_ seq: Int) {
        if expanded.contains(seq) { expanded.remove(seq) }
        else { expanded.insert(seq) }
    }

    /// Closing a run also folds its expanded calls, which would otherwise
    /// hold it open (`ConversationFold.runIsOpen`).
    private func toggleRun(_ id: Int, tools: [ConversationTurn]) {
        if ConversationFold.runIsOpen(tools, id: id, openRuns: openRuns,
                                      expanded: expanded) {
            openRuns.remove(id)
            for tool in tools { expanded.remove(tool.seq) }
        } else {
            openRuns.insert(id)
        }
    }
}

extension ConversationScreen where Header == EmptyView {
    init(agent: Agent, stopped: Bool, client: PhoneClient,
         retainedReply: PhoneReplyDraft? = nil, ask: String = "",
         typing: Bool = false) {
        self.init(agent: agent, stopped: stopped, client: client,
                  retainedReply: retainedReply, ask: ask, typing: typing,
                  header: { EmptyView() })
    }
}

/// A run of consecutive tool calls folded into one dim line —
/// `⚙ 5 tool calls · Bash ×3, Read, Grep`. A tap opens the calls, each
/// still its own `ConversationTurnRow` whose tap shows its brief and result.
struct ConversationRunRow: View {
    let tools: [ConversationTurn]
    let open: Bool
    let nickname: String
    let expanded: Set<Int>
    let result: (ConversationTurn) -> ConversationTurn?
    let onToggleRun: () -> Void
    let onToggleTool: (Int) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            DecryptButton(action: onToggleRun) {
                Text("\(open ? "▾" : "▸") \(ConversationFold.summary(tools))")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(ConversationFold.spoken(tools))
            .accessibilityHint(open ? "Hides the calls" : "Shows the calls")
            if open {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(tools) { tool in
                        ConversationTurnRow(
                            turn: tool,
                            nickname: nickname,
                            expanded: expanded.contains(tool.seq),
                            result: result(tool),
                            onToggle: { onToggleTool(tool.seq) }
                        )
                    }
                }
                .padding(.leading, 12)
            }
        }
        .fixedSize(horizontal: false, vertical: true)
    }
}

struct ConversationTurnRow: View {
    let turn: ConversationTurn
    let nickname: String
    let expanded: Bool
    let result: ConversationTurn?
    let onToggle: () -> Void
    /// Opens the picture sheet on one picture; nil where no sheet can
    /// open, and then no chips are drawn.
    var onImage: ((String) -> Void)? = nil

    var body: some View {
        let images = turn.kind == "agent" && onImage != nil
            ? ImageLinks.paths(in: turn.text) : []
        VStack(alignment: .leading, spacing: 6) {
            message
            if !images.isEmpty, let onImage {
                ConversationImageChips(paths: images, onOpen: { onImage(images[$0]) })
            }
        }
        .fixedSize(horizontal: false, vertical: true)
    }

    private var message: some View {
        let clock = ConversationRows.clock(turn.ts)
        return VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                rowBody
                Spacer(minLength: 8)
                if !clock.isEmpty {
                    Text(clock)
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.faint)
                }
            }
            if expanded, let result {
                Text(ConversationRows.resultLine(result))
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(ConversationRows.spoken(turn, nickname: nickname))
    }

    @ViewBuilder private var rowBody: some View {
        switch turn.kind {
        case "user":
            Text("> \(turn.text)")
                .font(Theme.mono(13))
                .foregroundStyle(Theme.phosphorBright)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        case "agent":
            VStack(alignment: .leading, spacing: 2) {
                Text(nickname)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                MarkdownText(source: turn.text, base: 13, mono: true)
                    .equatable()
            }
        case "tool":
            DecryptButton(action: onToggle) {
                Text(ConversationRows.toolLine(turn))
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .buttonStyle(.plain)
            if expanded {
                Text(turn.brief)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
        case "result":
            EmptyView()
        case "note":
            Text(turn.text)
                .font(Theme.mono(12))
                .italic()
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
        default:
            Text(turn.text)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// The pictures an agent's message names, as a row of chips under it
/// (`ImageLinks`). A tap opens the picture sheet on that one picture, and
/// only a tapped picture is ever asked of the Mac. Drawn outside the message's own
/// combined accessibility element, so each chip is its own button.
struct ConversationImageChips: View {
    let paths: [String]
    let onOpen: (Int) -> Void

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(Array(paths.enumerated()), id: \.offset) { pair in
                    DecryptButton(action: { onOpen(pair.offset) }) {
                        HStack(spacing: 6) {
                            Image(systemName: "photo")
                                .font(Theme.mono(11))
                                .accessibilityHidden(true)
                            Text(ImageLinks.name(pair.element))
                                .font(Theme.mono(11))
                        }
                        .foregroundStyle(Theme.phosphor)
                        .padding(.horizontal, 10)
                        .frame(minHeight: 44)
                        .overlay(RoundedRectangle(cornerRadius: 6)
                            .stroke(Theme.hair, lineWidth: 1))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Open picture \(ImageLinks.name(pair.element))")
                    .accessibilityHint(pair.element)
                }
            }
        }
    }
}

/// A picture from the Mac as a `UIImage`: an animated GIF becomes an
/// animated image (at most 300 frames), everything else decodes as it is.
enum PreviewPicture: Sendable {
    /// All frames of an animation together, decoded — the Mac's own
    /// `GIF_MAX_TOTAL_PIXELS`, held here too so an older Mac cannot exceed it.
    nonisolated static let maxAnimatedPixels = 40_000_000

    nonisolated static func image(_ preview: ImagePreview) -> UIImage? {
        guard let data = preview.pictureData else { return nil }
        if preview.format == "gif",
           let source = CGImageSourceCreateWithData(data as CFData, nil),
           CGImageSourceGetCount(source) > 1 {
            var frames: [UIImage] = []
            var total = 0.0
            var pixels = 0
            // Each frame is decoded at most 1024 pixels long, whatever the
            // file says: a phone screen needs no more.
            let options = [kCGImageSourceCreateThumbnailFromImageAlways: true,
                           kCGImageSourceThumbnailMaxPixelSize: 1024] as CFDictionary
            for i in 0..<min(CGImageSourceGetCount(source), 300) {
                guard let frame = CGImageSourceCreateThumbnailAtIndex(source, i, options) else { continue }
                // Every frame stays decoded while it plays: past the budget
                // the phone shows the first frame instead of running out.
                pixels += frame.width * frame.height
                if pixels > maxAnimatedPixels { frames = []; break }
                let props = CGImageSourceCopyPropertiesAtIndex(source, i, nil) as? [CFString: Any]
                let gif = props?[kCGImagePropertyGIFDictionary] as? [CFString: Any]
                let delay = (gif?[kCGImagePropertyGIFUnclampedDelayTime] as? Double)
                    ?? (gif?[kCGImagePropertyGIFDelayTime] as? Double) ?? 0.1
                total += max(delay, 0.02)
                frames.append(UIImage(cgImage: frame))
            }
            if frames.count > 1 { return UIImage.animatedImage(with: frames, duration: total) }
        }
        return UIImage(data: data)
    }
}

/// Pinch to zoom, double-tap to zoom in or back to fit, and an animated
/// GIF still moves: a `UIScrollView` around a `UIImageView`, which SwiftUI's
/// own `Image` cannot do.
struct ZoomablePicture: UIViewRepresentable {
    let image: UIImage
    let spoken: String

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> UIScrollView {
        let scroll = UIScrollView()
        scroll.minimumZoomScale = 1
        scroll.maximumZoomScale = 6
        scroll.showsHorizontalScrollIndicator = false
        scroll.showsVerticalScrollIndicator = false
        scroll.delegate = context.coordinator
        scroll.backgroundColor = .clear
        let view = UIImageView(image: image)
        view.contentMode = .scaleAspectFit
        view.frame = scroll.bounds
        view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.accessibilityIgnoresInvertColors = true
        scroll.addSubview(view)
        context.coordinator.imageView = view
        let tap = UITapGestureRecognizer(target: context.coordinator,
                                         action: #selector(Coordinator.doubleTap(_:)))
        tap.numberOfTapsRequired = 2
        scroll.addGestureRecognizer(tap)
        scroll.isAccessibilityElement = true
        scroll.accessibilityTraits = .image
        scroll.accessibilityLabel = spoken
        return scroll
    }

    func updateUIView(_ scroll: UIScrollView, context: Context) {
        scroll.accessibilityLabel = spoken
        guard context.coordinator.imageView?.image !== image else { return }
        scroll.setZoomScale(1, animated: false)
        context.coordinator.imageView?.image = image
    }

    final class Coordinator: NSObject, UIScrollViewDelegate {
        weak var imageView: UIImageView?

        func viewForZooming(in scrollView: UIScrollView) -> UIView? { imageView }

        @objc func doubleTap(_ tap: UITapGestureRecognizer) {
            guard let scroll = tap.view as? UIScrollView else { return }
            if scroll.zoomScale > 1.01 {
                scroll.setZoomScale(1, animated: true)
            } else {
                let point = tap.location(in: imageView)
                let size = CGSize(width: scroll.bounds.width / 2.5,
                                  height: scroll.bounds.height / 2.5)
                scroll.zoom(to: CGRect(x: point.x - size.width / 2,
                                       y: point.y - size.height / 2,
                                       width: size.width, height: size.height),
                            animated: true)
            }
        }
    }
}

/// The picture sheet: the one picture that was tapped, the Mac's facts
/// about the file and what was shrunk. It is asked of the Mac when the sheet
/// opens and at no other time; the phone keeps it in memory only, for a day
/// at most (`PhoneClient.cachedImage`), and offers no way to save it.
struct PhoneImageSheetView: View {
    @ObservedObject var client: PhoneClient
    let sessionId: String
    let path: String
    @State private var preview: ImagePreview?
    @State private var picture: UIImage?
    @State private var failed = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(path)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
            content
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            facts
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .task(id: path) { await load() }
    }

    @ViewBuilder private var content: some View {
        if let picture, let preview {
            ZoomablePicture(image: picture, spoken: ImagePreviewWords.spoken(preview))
        } else if let preview, !preview.available {
            message(preview.reason.isEmpty ? "The Mac could not show this picture" : preview.reason,
                    retry: false)
        } else if preview != nil {
            message("The phone could not draw this picture", retry: false)
        } else if failed {
            message("No answer from the Mac", retry: true)
        } else {
            Text("// asking the Mac…")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
        }
    }

    private func message(_ words: String, retry: Bool) -> some View {
        VStack(spacing: 12) {
            Text(words)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            if retry {
                DecryptButton(action: { Task { await load() } }) {
                    Text("Try again")
                        .font(Theme.mono(12))
                        .frame(minHeight: 44)
                }
                .buttonStyle(.plain)
                .foregroundStyle(Theme.phosphor)
            }
        }
        .padding(20)
    }

    @ViewBuilder private var facts: some View {
        if let preview, preview.available {
            VStack(alignment: .leading, spacing: 4) {
                Text(ImagePreviewWords.meta(preview))
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                let reduced = ImagePreviewWords.reducedLine(preview)
                if !reduced.isEmpty {
                    Text(reduced)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    private func load() async {
        failed = false
        if preview == nil, let held = client.cachedImage(session: sessionId, path: path) {
            await accept(held)
            return
        }
        guard let fresh = await client.imagePreview(session: sessionId, path: path) else {
            if !Task.isCancelled, preview == nil { failed = true }
            return
        }
        await accept(fresh)
    }

    /// Decoding (an animation's every frame) happens off the main actor.
    private func accept(_ fresh: ImagePreview) async {
        let drawn = await Task.detached(priority: .userInitiated) {
            PreviewPicture.image(fresh)
        }.value
        preview = fresh
        picture = drawn
    }
}
