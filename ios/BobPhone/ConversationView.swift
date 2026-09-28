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
    /// The agent is working right now (the live `running` bucket): the page
    /// is followed quickly and `now` is drawn under the turns.
    var running: Bool = false
    /// What it is doing, in the fleet row's words (`PhoneAgentFacts.head`).
    var now: String = ""
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
    /// The picture opened from a chip, in its own sheet over this one: the
    /// conversation under it stays mounted, so closing it returns to the
    /// same tab at the same scroll position.
    @State private var picture: PicturePick?
    /// Rows that arrived while the person was scrolled up to read.
    @State private var unseen: Int
    /// Stepped to ask the page to go to its foot.
    @State private var jump: Int
    // Computed, not stored: a generic type cannot hold a static stored
    // property, and the archive build refuses it.
    static var windowStep: Int { 300 }

    init(agent: Agent, stopped: Bool, client: PhoneClient,
         retainedReply: PhoneReplyDraft? = nil,
         ask: String = "",
         running: Bool = false,
         now: String = "",
         typing: Bool = false,
         @ViewBuilder header: () -> Header) {
        self.agent = agent
        self.stopped = stopped
        self.client = client
        self.retainedReply = retainedReply
        self.ask = ask
        self.typing = typing
        self.running = running
        self.now = now
        self.header = header()
        self._expanded = State(initialValue: [])
        self._openRuns = State(initialValue: [])
        self._atBottom = State(initialValue: true)
        self._onScreen = State(initialValue: false)
        self._window = State(initialValue: Self.windowStep)
        self._unseen = State(initialValue: 0)
        self._jump = State(initialValue: 0)
    }

    /// Messages the person sent that the journal has not recorded yet.
    private var echoes: [ConversationLive.Echo] {
        ConversationLive.echoes(receipts: client.receipts.receipts,
                                sessionId: agent.sessionId, turns: turns,
                                now: Date().timeIntervalSince1970)
    }

    private var following: Bool {
        ConversationLive.follows(running: running, echoes: echoes.count)
    }

    private var nowLine: String {
        ConversationLive.nowLine(running: running, head: now)
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
            if !atBottom, unseen > 0 {
                newRow
            }
            Rectangle().fill(Theme.hair).frame(height: 1)
            AnswerBox(agent: agent, stopped: stopped, client: client,
                      retainedReply: retainedReply, part: .composer)
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(Theme.bg)
        }
        .picturePopup($picture, client: client)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear {
            onScreen = true
            client.watchConversation(agent.sessionId)
            Task { await client.catchUpConversation(agent.sessionId) }
        }
        .task(id: "\(agent.sessionId)|\(following)") {
            // The quick follow: at home a small page about every second
            // while the agent works or a sent message has not shown; away,
            // the Mac's pushed picture asks instead (`PhoneClient.tookPush`).
            client.followConversation(following)
            guard following else { return }
            while !Task.isCancelled {
                if !client.knowsItIsAway { await client.followConversationOnce() }
                try? await Task.sleep(nanoseconds: ConversationLive.followPause)
            }
        }
        .onDisappear {
            client.followConversation(false)
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
                        ForEach(echoes) { echo in
                            ConversationEchoRow(echo: echo)
                        }
                        if !nowLine.isEmpty {
                            ConversationNowLine(text: nowLine, seed: agent.sessionId)
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
                    .onAppear { atBottom = true; unseen = 0 }
                    .onDisappear { atBottom = false }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            // A drag down the page puts the keyboard away.
            .scrollDismissesKeyboard(.interactively)
            .defaultScrollAnchor(.bottom)
            .onAppear { proxy.scrollTo(Self.answerAnchor, anchor: .bottom) }
            .onChange(of: turns.count) { old, new in
                guard atBottom else {
                    if new > old { unseen += new - old }
                    return
                }
                proxy.scrollTo(Self.answerAnchor, anchor: .bottom)
            }
            .onChange(of: echoes.count) { old, new in
                // The person just pressed SEND: their words are what they
                // want to see, wherever they had scrolled.
                if new > old { jump += 1 }
            }
            .onChange(of: jump) { _, _ in
                // One scroll for the two asks to go to the foot: a message
                // just sent, and the "↓ n new" row pressed.
                unseen = 0
                Motion.animate(.easeOut(duration: 0.2), reduced: reduceMotion) {
                    proxy.scrollTo(Self.answerAnchor, anchor: .bottom)
                }
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
                onImage: { path in picture = PicturePick(sessionId: agent.sessionId, path: path) }
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

    /// "↓ 3 new": rows arrived while the person was scrolled up to read.
    /// A stack sibling above the composer, never painted over the page; a
    /// press goes to the foot.
    private var newRow: some View {
        DecryptButton(action: { jump += 1 }) {
            Text(ConversationLive.newPill(unseen))
                .font(Theme.mono(11))
                .foregroundStyle(Theme.phosphor)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 4)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(unseen == 1 ? "1 new turn, go to it"
                                        : "\(unseen) new turns, go to them")
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
         running: Bool = false, now: String = "", typing: Bool = false) {
        self.init(agent: agent, stopped: stopped, client: client,
                  retainedReply: retainedReply, ask: ask,
                  running: running, now: now, typing: typing,
                  header: { EmptyView() })
    }
}

/// A message the person just sent, drawn as their turn is (`> words`) but
/// dimmer and marked `sending` or `delivered`, until the journal records
/// it and the real turn takes its place (`ConversationLive.echoes`).
struct ConversationEchoRow: View {
    let echo: ConversationLive.Echo

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(ConversationLive.mark(echo))
                .font(Theme.mono(10))
                .foregroundStyle(echo.delivered ? Theme.phosphor : Theme.dim)
            Text("> \(echo.text)")
                .font(Theme.mono(13))
                .foregroundStyle(Theme.phosphorBright)
                .opacity(0.7)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("You, \(ConversationLive.mark(echo)): \(echo.text)")
    }
}

/// What the agent is doing right now, one dim line under the turns while
/// it works — the fleet row's words, with the caret that says it is live.
struct ConversationNowLine: View {
    let text: String
    let seed: String

    var body: some View {
        HStack(spacing: 8) {
            Text("// \(text)")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
            AgentChatterView(.caret, wait: .opening, seed: seed, spoken: "The agent is working")
                .id(seed)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(text)
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
                    DecryptButton(action: {
                        // Put the reply keyboard away first: UIKit would
                        // hand focus back when the picture closes, and the
                        // rising keyboard scrolls the page to the bottom.
                        UIApplication.shared.sendAction(
                            #selector(UIResponder.resignFirstResponder),
                            to: nil, from: nil, for: nil)
                        onOpen(pair.offset)
                    }) {
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

/// One picture a chip opened: which session's project, which path.
struct PicturePick: Identifiable, Equatable {
    let sessionId: String
    let path: String
    var id: String { sessionId + "/" + path }
}

/// The picture's own sheet, presented by the conversation that opened it
/// rather than pushed onto the phone's one sheet trail: a rung on that
/// trail replaces the agent screen, which comes back rebuilt on Main at
/// the top. Close or a drag down returns to the conversation untouched.
struct PicturePopup: View {
    @ObservedObject var client: PhoneClient
    let pick: PicturePick
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 10) {
                Text(ImageLinks.name(pick.path))
                    .font(Theme.mono(14, weight: .medium))
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    .accessibilityAddTraits(.isHeader)
                DecryptButton(action: { dismiss() }) {
                    Text("Close").font(Theme.mono(12))
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(minHeight: 44)
                }
                .buttonStyle(.plain)
                .foregroundStyle(Theme.phosphor)
            }
            .padding(.horizontal, 14)
            .padding(.top, 8)
            Rectangle().fill(Theme.hair).frame(height: 1)
                .padding(.horizontal, 14)
            PhoneImageSheetView(client: client, sessionId: pick.sessionId, path: pick.path)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(Theme.bg)
        .presentationDetents([.large])
        .presentationDragIndicator(.visible)
        .presentationBackground(Theme.bg)
    }
}

extension View {
    /// Presents a chip's picture over this view, leaving this view mounted.
    func picturePopup(_ pick: Binding<PicturePick?>, client: PhoneClient) -> some View {
        sheet(item: pick) { PicturePopup(client: client, pick: $0) }
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
