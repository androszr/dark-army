import AppKit
import SwiftTerm

// The cyber cursor: a Matrix-styled cursor for the hosted terminal.
//
// The look and its timing constants are a port of the "cyber" effect from
// ghostty-cursor-fx (https://github.com/Avarce/ghostty-cursor-fx,
// shaders/cyber-cursor.glsl and cursor-effects/cyber.conf), MIT licence,
// Copyright (c) 2026 AONIR; the licence text is on the third-party notices
// page (THIRD_PARTY_NOTICES.md, "Ported source"). Upstream is a Ghostty GLSL
// custom shader driven by cursor-colour hooks and cannot run inside
// SwiftTerm, so this file reimplements the look natively: a transparent
// overlay above the terminal view, drawn in CoreGraphics, triggered from Dark
// Army's own session state instead of the colour-bit hack. Upstream finds the
// input box by pixel colour; this port reads the character grid.
//
// Everything here runs on the AppKit main thread. The overlay takes no click
// and no key (`hitTest` is nil), invalidates only the rects it touched and
// stops its timer when there is nothing to see: hidden, scrolled back, quiet
// for half a minute, or Reduce Motion. The contract is
// `docs/panel-window-contract.md`, "The hosted terminal wears the cyber
// cursor".

/// The ported constants, in upstream's names.
enum CyberLook {
    // Colours. Upstream states RAIN_COLOR and TRAIL_COLOR in linear light;
    // they are converted to sRGB once.
    static let cursorHex: UInt32 = 0x50e080
    static let rainLinear: (r: Double, g: Double, b: Double) = (0.35, 1.0, 0.55)
    static let trailLinear: (r: Double, g: Double, b: Double) = (0.0, 0.55, 0.45)
    static let brightness = 1.0

    // The cursor's look.
    static let glowSize: CGFloat = 0.10      // cell heights
    static let glowCore = 0.7
    static let cursorGlow = 0.6
    static let cursorRain = 1.2              // rows per second
    static let cursorShade = 0.3
    static let scanPeriod: TimeInterval = 2.6
    static let breathPeriod: TimeInterval = 3.0

    // Typing.
    static let decodeTime: TimeInterval = 0.22
    static let wordWave: TimeInterval = 0.035

    // Enter and done.
    static let enterTime: TimeInterval = 1.5
    static let enterSpread: TimeInterval = 0.35
    static let rainSpeed = 6.0               // cells per second
    static let trail = 6
    static let density = 0.5
    static let glyphSize: CGFloat = 0.62
    static let settle: TimeInterval = 0.04

    static func srgb(fromLinear v: Double) -> Double {
        let c = max(0, min(1, v))
        return c <= 0.0031308 ? 12.92 * c : 1.055 * pow(c, 1 / 2.4) - 0.055
    }

    private static func color(_ lin: (r: Double, g: Double, b: Double)) -> NSColor {
        NSColor(srgbRed: CGFloat(srgb(fromLinear: lin.r)),
                green: CGFloat(srgb(fromLinear: lin.g)),
                blue: CGFloat(srgb(fromLinear: lin.b)), alpha: 1)
    }

    static let cursor = TerminalLook.nsColor(cursorHex)
    static let rain = color(rainLinear)
    static let trailColor = color(trailLinear)
    /// The ink the cursor's falling glyphs are drawn in: the terminal's own
    /// background, so they read as dark characters cut into the green block.
    static let shade = TerminalLook.backgroundNS
}

// MARK: - Glyphs

/// The made-up characters: a 14-stroke segment alphabet, as upstream's
/// `STROKES[14]` / `glyphDist`. A glyph is a bit mask over these segments.
enum CyberGlyph {
    /// Each stroke as (x0, y0, x1, y1) in a unit box, y up.
    static let strokes: [(Double, Double, Double, Double)] = [
        (0, 1, 1, 1), (0, 0, 1, 0),
        (0, 1, 0, 0.5), (0, 0.5, 0, 0),
        (1, 1, 1, 0.5), (1, 0.5, 1, 0),
        (0, 0.5, 0.5, 0.5), (0.5, 0.5, 1, 0.5),
        (0.5, 1, 0.5, 0.5), (0.5, 0.5, 0.5, 0),
        (0, 1, 0.5, 0.5), (1, 1, 0.5, 0.5),
        (0, 0, 0.5, 0.5), (1, 0, 0.5, 0.5),
    ]

    static func hash(_ x: UInt32) -> UInt32 {
        var h = x &+ 0x9e3779b9
        h = (h ^ (h >> 16)) &* 0x85ebca6b
        h = (h ^ (h >> 13)) &* 0xc2b2ae35
        return h ^ (h >> 16)
    }

    static func hash(_ a: Int, _ b: Int, _ c: Int = 0) -> UInt32 {
        hash(UInt32(truncatingIfNeeded: a) &* 73856093
             ^ UInt32(truncatingIfNeeded: b) &* 19349663
             ^ UInt32(truncatingIfNeeded: c) &* 83492791)
    }

    /// 0 ..< 1, from a hash.
    static func unit(_ h: UInt32) -> Double { Double(h) / 4294967296.0 }

    /// A mask with at least four strokes lit, so no glyph is a stray dot.
    static func mask(seed: UInt32) -> UInt16 {
        var m = UInt16(truncatingIfNeeded: hash(seed) & 0x3fff)
        if m.nonzeroBitCount < 4 { m |= 0b0000_0011_0100_0001 }
        return m
    }

    static func draw(_ mask: UInt16, in rect: CGRect, color: NSColor, alpha: CGFloat,
                     context ctx: CGContext) {
        guard alpha > 0.01, rect.width > 1, rect.height > 1 else { return }
        ctx.saveGState()
        ctx.setStrokeColor(color.withAlphaComponent(alpha).cgColor)
        ctx.setLineWidth(max(1, rect.height * 0.09))
        ctx.setLineCap(.round)
        for (i, s) in strokes.enumerated() where mask & (1 << UInt16(i)) != 0 {
            ctx.move(to: CGPoint(x: rect.minX + CGFloat(s.0) * rect.width,
                                 y: rect.minY + CGFloat(s.1) * rect.height))
            ctx.addLine(to: CGPoint(x: rect.minX + CGFloat(s.2) * rect.width,
                                    y: rect.minY + CGFloat(s.3) * rect.height))
        }
        ctx.strokePath()
        ctx.restoreGState()
    }
}

// MARK: - Geometry

/// Where things are: the cell size, the cursor's rect, the input box. Pure.
enum CyberGeometry {
    /// SwiftTerm's own formula (`computeFontDimensions`): the advance of `W`
    /// and the ceiling of ascent + descent + leading, times the line spacing.
    static func cell(font: NSFont, lineSpacing: CGFloat = 1) -> CGSize {
        let ct = font as CTFont
        let height = ceil((CTFontGetAscent(ct) + CTFontGetDescent(ct)
                           + CTFontGetLeading(ct)) * lineSpacing)
        let width = font.advancement(forGlyph: font.glyph(withName: "W")).width
        return CGSize(width: width, height: height)
    }

    /// The cell at (col, row) on the unflipped `TerminalView`, rows counted
    /// from the top: `y = viewHeight - cellHeight * (row + 1)`.
    static func cursorRect(col: Int, row: Int, cell: CGSize,
                           viewHeight: CGFloat) -> CGRect {
        CGRect(x: CGFloat(col) * cell.width,
               y: viewHeight - cell.height * CGFloat(row + 1),
               width: cell.width, height: cell.height)
    }

    static let boxCharacters: Set<Character> = ["─", "━", "═"]

    /// A row that is a horizontal rule: at least 60% of the columns inked and
    /// at least 90% of the ink in `─ ━ ═` (so an end cap does not spoil it).
    static func isBoxLine(_ row: String, cols: Int) -> Bool {
        guard cols > 0 else { return false }
        var ink = 0
        var rule = 0
        for ch in row where ch != " " && ch != "\u{0}" {
            ink += 1
            if boxCharacters.contains(ch) { rule += 1 }
        }
        guard ink > 0 else { return false }
        return Double(ink) >= 0.6 * Double(cols) && Double(rule) >= 0.9 * Double(ink)
    }

    /// The rule above the cursor within `reachAbove` rows and the one below
    /// within `reachBelow`, nearest first. Either may be absent.
    static func boxLines(rows: [String], cursorRow: Int, cols: Int,
                         reachAbove: Int = 4, reachBelow: Int = 2)
        -> (above: Int?, below: Int?) {
        var above: Int?
        var below: Int?
        if reachAbove >= 1 {
            for k in 1...reachAbove {
                let r = cursorRow - k
                if r >= 0, r < rows.count, isBoxLine(rows[r], cols: cols) { above = r; break }
            }
        }
        if reachBelow >= 1 {
            for k in 1...reachBelow {
                let r = cursorRow + k
                if r >= 0, r < rows.count, isBoxLine(rows[r], cols: cols) { below = r; break }
            }
        }
        return (above, below)
    }

    /// The rows the rain falls through: the box's inside when both lines are
    /// known, else the cursor's row and one either side (upstream's fallback).
    static func rainSpan(above: Int?, below: Int?, cursorRow: Int,
                         rows: Int) -> ClosedRange<Int> {
        let last = max(rows - 1, 0)
        if let above, let below, below - above >= 2 {
            return max(above + 1, 0)...min(below - 1, last)
        }
        let lo = min(max(cursorRow - 1, 0), last)
        let hi = min(max(cursorRow + 1, lo), last)
        return lo...hi
    }
}

// MARK: - Triggers

/// What a keystroke and a bucket change mean. Pure.
enum CyberTriggers {
    enum Input: Equatable { case enter, typed, other }
    enum Hop: Equatable { case enter, done, none }

    /// How long after a local Enter the snapshot's `running` hop is taken as
    /// the same event.
    static let enterSuppress: TimeInterval = 3

    /// Exactly CR is Enter; ESC CR (Shift/Option-Return) is not. One
    /// printable character (ASCII or one UTF-8 character) is a typed letter;
    /// an arrow sequence or a paste is neither.
    static func classify(_ bytes: [UInt8]) -> Input {
        if bytes == [0x0d] { return .enter }
        guard !bytes.isEmpty, bytes.count <= 8,
              let text = String(bytes: bytes, encoding: .utf8), text.count == 1,
              text.unicodeScalars.allSatisfy({ $0.value >= 0x20 && $0.value != 0x7f
                  && $0.properties.generalCategory != .control })
        else { return .other }
        return .typed
    }

    /// The Enter and done signals read off the agents snapshot: into
    /// `running` from any other bucket is an Enter (unless a local Enter just
    /// played); out of `running` into `waiting` or `sleeping` is done.
    static func hop(from: String?, to: String?, localEnterAt: Date?, now: Date) -> Hop {
        guard let from, let to, from != to else { return .none }
        if to == "running" {
            if let at = localEnterAt {
                let age = now.timeIntervalSince(at)
                if age >= 0 && age < enterSuppress { return .none }
            }
            return .enter
        }
        if from == "running" && (to == "waiting" || to == "sleeping") { return .done }
        return .none
    }
}

// MARK: - Scheduler

/// How often the overlay may draw. Pure; `nil` means the timer is off.
enum CyberScheduler {
    /// SwiftTerm reports position 0 for a buffer with no scrollback at all, so
    /// the position alone cannot say "scrolled back": `canScroll` (false on
    /// the alternate screen and when nothing is above) must agree.
    static func isScrolledBack(canScroll: Bool, position: Double) -> Bool {
        canScroll && position < 1
    }

    static let liveSeconds: TimeInterval = 30
    static let burstInterval: TimeInterval = 1.0 / 60.0
    static let liveInterval: TimeInterval = 1.0 / 15.0

    static func interval(now: Date, lastActivity: Date, bursting: Bool, visible: Bool,
                         scrolledBack: Bool, reduced: Bool) -> TimeInterval? {
        guard visible, !scrolledBack, !reduced else { return nil }
        if bursting { return burstInterval }
        if now.timeIntervalSince(lastActivity) < liveSeconds { return liveInterval }
        return nil
    }
}

// MARK: - Episode

/// One Enter rain or one done burst, as a function of time. Pure.
struct CyberEpisode {
    enum Kind: Equatable { case enter, done }

    let kind: Kind
    let start: Date
    /// The rows the rain falls through (inside the box).
    let span: ClosedRange<Int>
    /// The rows the sweeps run along: the box's lines, or the cursor's row.
    let sweepRows: [Int]
    let cols: Int

    static var duration: TimeInterval { CyberLook.enterTime }
    /// The done burst's two sweeps meet at this moment.
    static let meetTime: TimeInterval = 0.9

    func elapsed(_ now: Date) -> TimeInterval { now.timeIntervalSince(start) }
    func isActive(_ now: Date) -> Bool {
        let t = elapsed(now)
        return t >= 0 && t < Self.duration
    }

    /// 1 until the last third of a second, then down to 0.
    func fade(_ now: Date) -> Double {
        let left = Self.duration - elapsed(now)
        return max(0, min(1, left / 0.3))
    }

    struct RainCell: Equatable {
        let col: Int
        let row: Int
        let brightness: Double
        let isHead: Bool
        let glyph: UInt16
    }

    /// Sharp glyph rain, column by column: about half the columns carry a
    /// drop, each column starting a little after the last.
    func rainCells(_ now: Date) -> [RainCell] {
        guard kind == .enter, isActive(now) else { return [] }
        let t0 = elapsed(now)
        let spanLen = span.count
        let period = Double(spanLen + CyberLook.trail) / CyberLook.rainSpeed + 0.1
        let fade = self.fade(now)
        var out: [RainCell] = []
        for c in 0..<cols {
            guard CyberGlyph.unit(CyberGlyph.hash(c, 11)) < CyberLook.density else { continue }
            let delay = CyberGlyph.unit(CyberGlyph.hash(c, 23)) * CyberLook.enterSpread
            let t = t0 - delay
            if t < 0 { continue }
            let phase = t.truncatingRemainder(dividingBy: period)
            let head = phase * CyberLook.rainSpeed
            for k in 0..<CyberLook.trail {
                let pos = Int(floor(head)) - k
                guard pos >= 0, pos < spanLen else { continue }
                let b = (1 - Double(k) / Double(CyberLook.trail)) * fade
                let seed = CyberGlyph.hash(c, span.lowerBound + pos, Int(t0 * 14))
                out.append(RainCell(col: c, row: span.lowerBound + pos, brightness: b,
                                    isHead: k == 0, glyph: CyberGlyph.mask(seed: seed)))
            }
        }
        return out
    }

    struct Sweep: Equatable {
        /// Columns, fractional; `x0 < x1`.
        let x0: Double
        let x1: Double
        /// 1 at the leading edge.
        let leadsRight: Bool
    }

    /// The bright bars along the sweep rows right now.
    func sweeps(_ now: Date) -> [Sweep] {
        guard isActive(now) else { return [] }
        let t = elapsed(now)
        let width = max(8.0, Double(cols) * 0.2)
        let total = Double(cols)
        switch kind {
        case .enter:
            let p = min(1, t / (Self.duration * 0.8))
            let head = p * (total + width)
            return [Sweep(x0: max(0, head - width), x1: min(total, head), leadsRight: true)]
        case .done:
            let p = min(1, t / Self.meetTime)
            let reach = total / 2 * p
            return [Sweep(x0: max(0, reach - width), x1: reach, leadsRight: true),
                    Sweep(x0: total - reach, x1: min(total, total - reach + width),
                          leadsRight: false)]
        }
    }

    /// The flare where the done sweeps meet, 0 before and fading after.
    func flare(_ now: Date) -> Double {
        guard kind == .done, isActive(now) else { return 0 }
        let t = elapsed(now)
        guard t >= Self.meetTime else { return 0 }
        return max(0, 1 - (t - Self.meetTime) / (Self.duration - Self.meetTime))
    }

    /// Every row the episode can touch, for the invalidation rect.
    var touchedRows: ClosedRange<Int> {
        let all = sweepRows + [span.lowerBound, span.upperBound]
        let lo = all.min() ?? span.lowerBound
        let hi = all.max() ?? span.upperBound
        return max(lo - 1, 0)...(hi + 1)
    }
}

// MARK: - Overlay

/// The transparent layer above the terminal. It paints the living cursor, the
/// decrypting letters and the Enter/done episodes, and nothing else.
final class CyberCursorOverlay: NSView {
    /// A letter being decrypted: random glyphs over the cell it will land in.
    struct Decode {
        let col: Int
        let row: Int
        let start: Date
        let end: Date
    }

    // Sources, installed by `attach(to:)` and replaced by tests.
    var cursorProvider: () -> (col: Int, row: Int, cols: Int, rows: Int)? = { nil }
    var rowTextProvider: () -> [String] = { [] }
    var caretPresentProvider: () -> Bool = { true }
    var clock: () -> Date = Date.init

    var cell = CyberGeometry.cell(font: TerminalLook.font)

    private(set) var enabled = true
    private(set) var reduced = false
    private(set) var visible = true
    private(set) var scrolledBack = false
    private(set) var stopped = false
    private(set) var lastActivity = Date()
    private(set) var episode: CyberEpisode?
    private(set) var decodes: [Decode] = []
    private var lastBucket: String?
    private var localEnterAt: Date?
    private var timer: Timer?
    private var timerInterval: TimeInterval = 0
    private var lastDirty = CGRect.null

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        autoresizingMask = [.width, .height]
    }

    required init?(coder: NSCoder) { nil }

    deinit { timer?.invalidate() }

    // MARK: AppKit contract: never takes a click, a key or a place in the focus chain

    override func hitTest(_ point: NSPoint) -> NSView? { nil }
    override var isOpaque: Bool { false }
    override var acceptsFirstResponder: Bool { false }
    override func isAccessibilityElement() -> Bool { false }

    // MARK: attachment

    func attach(to view: TerminalView) {
        cell = CyberGeometry.cell(font: view.font, lineSpacing: view.lineSpacing)
        frame = view.bounds
        cursorProvider = { [weak view] in
            guard let term = view?.getTerminal() else { return nil }
            let loc = term.getCursorLocation()
            return (loc.x, loc.y, term.cols, term.rows)
        }
        rowTextProvider = { [weak view] in
            guard let term = view?.getTerminal() else { return [] }
            return (0..<term.rows).map { r in
                var line = ""
                for c in 0..<term.cols {
                    let ch = term.getCharacter(col: c, row: r) ?? " "
                    line.append(ch == "\u{0}" ? " " : ch)
                }
                return line
            }
        }
        caretPresentProvider = { [weak self, weak view] in
            guard let self, let view else { return false }
            // The caret is the one cell-sized subview that is neither this
            // overlay nor the scroller; SwiftTerm removes it when the cursor
            // is hidden or off the screen.
            return view.subviews.contains { sub in
                sub !== self && !(sub is NSScroller)
                    && abs(sub.frame.height - self.cell.height) < 1.5
                    && sub.frame.width >= self.cell.width - 1.5
                    && sub.frame.width <= self.cell.width * 2 + 1.5
            }
        }
        if superview !== view {
            view.addSubview(self, positioned: .above, relativeTo: nil)
        }
        refresh(clock())
    }

    /// A new session in the same pane: forget everything the old one did.
    func reset() {
        episode = nil
        decodes = []
        lastBucket = nil
        localEnterAt = nil
        lastActivity = clock()
        refresh(lastActivity)
    }

    func stop() {
        stopped = true
        episode = nil
        decodes = []
        timer?.invalidate()
        timer = nil
        invalidate(clock())
    }

    // MARK: state in

    func setEnabled(_ on: Bool) {
        guard on != enabled else { return }
        enabled = on
        if !on { episode = nil; decodes = [] } else { lastActivity = clock() }
        refresh(clock())
    }

    func setReduced(_ on: Bool) {
        guard on != reduced else { return }
        reduced = on
        if on { episode = nil; decodes = [] }
        refresh(clock())
    }

    func setVisible(_ on: Bool) {
        guard on != visible else { return }
        visible = on
        if !on { episode = nil; decodes = [] } else { lastActivity = clock() }
        refresh(clock())
    }

    func setScrolledBack(_ on: Bool) {
        guard on != scrolledBack else { return }
        scrolledBack = on
        if on { episode = nil; decodes = [] } else { lastActivity = clock() }
        refresh(clock())
    }

    /// A key about to go up the pty, called before the paste is chunked.
    func noteInput(_ bytes: [UInt8]) {
        guard enabled, !stopped else { return }
        let now = clock()
        lastActivity = now
        switch CyberTriggers.classify(bytes) {
        case .enter:
            localEnterAt = now
            startEpisode(.enter, now)
        case .typed:
            if bytes != [0x20] { addDecode(now) }
        case .other:
            break
        }
        refresh(now)
    }

    /// Bytes were fed to the visible terminal.
    func noteOutput() {
        guard enabled, !stopped else { return }
        let now = clock()
        lastActivity = now
        refresh(now)
    }

    /// The agents snapshot's bucket for this session.
    func noteBucket(_ bucket: String?) {
        let now = clock()
        let hop = CyberTriggers.hop(from: lastBucket, to: bucket,
                                    localEnterAt: localEnterAt, now: now)
        lastBucket = bucket
        guard enabled, !stopped else { return }
        switch hop {
        case .enter: startEpisode(.enter, now)
        case .done: startEpisode(.done, now)
        case .none: return
        }
        lastActivity = now
        refresh(now)
    }

    // MARK: episodes and decodes

    private var drawable: Bool {
        enabled && visible && !scrolledBack && !stopped
    }

    private func startEpisode(_ kind: CyberEpisode.Kind, _ now: Date) {
        guard drawable, !reduced, let cur = cursorProvider() else { return }
        // The grid is read once per episode, on main, never per frame.
        let text = rowTextProvider()
        let lines = CyberGeometry.boxLines(rows: text, cursorRow: cur.row, cols: cur.cols)
        let span = CyberGeometry.rainSpan(above: lines.above, below: lines.below,
                                          cursorRow: cur.row, rows: cur.rows)
        // No box found: the fallback is the cursor's own row, which means
        // nothing when the cursor is hidden.
        if lines.above == nil && lines.below == nil && !caretPresentProvider() { return }
        let sweepRows = [lines.above, lines.below].compactMap { $0 }
        episode = CyberEpisode(kind: kind, start: now, span: span,
                               sweepRows: sweepRows.isEmpty ? [cur.row] : sweepRows,
                               cols: cur.cols)
    }

    private func addDecode(_ now: Date) {
        // A hidden cursor is parked somewhere meaningless; decrypting there
        // would paint over text.
        guard drawable, !reduced, caretPresentProvider(),
              let cur = cursorProvider() else { return }
        decodes.removeAll { $0.end <= now }
        // A word's letters settle a hair apart, as upstream's WORD_WAVE.
        let wave = CyberLook.wordWave * Double(decodes.count)
        decodes.append(Decode(col: cur.col, row: cur.row, start: now,
                              end: now.addingTimeInterval(CyberLook.decodeTime + wave)))
        if decodes.count > 24 { decodes.removeFirst(decodes.count - 24) }
    }

    private func prune(_ now: Date) {
        decodes.removeAll { $0.end <= now }
        if let e = episode, !e.isActive(now) {
            // Its rect is still in `lastDirty`; the next invalidation clears it.
            episode = nil
        }
    }

    /// An episode or a decode is running.
    var bursting: Bool { episode != nil || !decodes.isEmpty }

    // MARK: frames

    private func tick() {
        timer = nil
        timerInterval = 0
        refresh(clock())
    }

    /// SwiftTerm re-adds its caret with a plain `addSubview` (every cursor
    /// show), which would put an opaque block above this layer. Stay last.
    func raise() {
        guard let parent = superview, parent.subviews.last !== self else { return }
        parent.addSubview(self, positioned: .above, relativeTo: nil)
    }

    private func refresh(_ now: Date) {
        raise()
        prune(now)
        invalidate(now)
        reschedule(now)
    }

    private func reschedule(_ now: Date) {
        guard !stopped, enabled,
              let dt = CyberScheduler.interval(now: now, lastActivity: lastActivity,
                                               bursting: bursting, visible: visible,
                                               scrolledBack: scrolledBack, reduced: reduced)
        else {
            timer?.invalidate()
            timer = nil
            timerInterval = 0
            return
        }
        if timer != nil && timerInterval == dt { return }
        timer?.invalidate()
        let t = Timer(timeInterval: dt, repeats: false) { [weak self] _ in self?.tick() }
        timer = t
        timerInterval = dt
        RunLoop.main.add(t, forMode: .common)
    }

    /// Whether a timer is waiting; the stop rule's observable side.
    var isTicking: Bool { timer != nil }

    private var glowStep: CGFloat { max(1.5, cell.height * CyberLook.glowSize) }

    private func cursorCell() -> CGRect? {
        guard drawable, caretPresentProvider(), let cur = cursorProvider() else { return nil }
        return CyberGeometry.cursorRect(col: cur.col, row: cur.row, cell: cell,
                                        viewHeight: bounds.height)
    }

    private func currentDirty() -> CGRect {
        guard drawable else { return .null }
        var rect = CGRect.null
        if let c = cursorCell() { rect = rect.union(c.insetBy(dx: -glowStep * 5, dy: -glowStep * 5)) }
        for d in decodes {
            rect = rect.union(CyberGeometry.cursorRect(col: d.col, row: d.row, cell: cell,
                                                       viewHeight: bounds.height)
                .insetBy(dx: -1, dy: -1))
        }
        if let e = episode {
            let rows = e.touchedRows
            let top = bounds.height - cell.height * CGFloat(rows.lowerBound)
            let bottom = bounds.height - cell.height * CGFloat(rows.upperBound + 1)
            rect = rect.union(CGRect(x: 0, y: bottom,
                                     width: cell.width * CGFloat(e.cols), height: top - bottom))
        }
        return rect
    }

    /// Rect-scoped invalidation only: the union of what was drawn last time
    /// and what will be drawn now, never the whole view.
    private func invalidate(_ now: Date) {
        let dirty = currentDirty()
        let union = lastDirty.union(dirty)
        lastDirty = dirty
        guard !union.isNull, !union.isEmpty else { return }
        setNeedsDisplay(union.intersection(bounds))
    }

    // MARK: drawing

    override func draw(_ dirtyRect: NSRect) {
        guard let ctx = NSGraphicsContext.current?.cgContext else { return }
        ctx.clear(dirtyRect)
        guard drawable else { return }
        ctx.saveGState()
        ctx.clip(to: dirtyRect)
        let now = clock()
        if let e = episode, e.isActive(now) { drawEpisode(e, now, ctx) }
        for d in decodes where d.end > now { drawDecode(d, now, ctx) }
        if let c = cursorCell() { drawCursor(c, now, ctx) }
        ctx.restoreGState()
    }

    private func rowRect(_ row: Int) -> CGRect {
        CGRect(x: 0, y: bounds.height - cell.height * CGFloat(row + 1),
               width: cell.width * CGFloat(cursorProvider()?.cols ?? 0), height: cell.height)
    }

    private func drawCursor(_ c: CGRect, _ now: Date, _ ctx: CGContext) {
        let t = now.timeIntervalSinceReferenceDate
        // Breathing glow: with Reduce Motion it sits at its midpoint.
        let breath = reduced ? 0.5 : 0.5 + 0.5 * sin(2 * .pi * t / CyberLook.breathPeriod)
        let glow = CyberLook.cursorGlow * (0.45 + 0.55 * breath) * CyberLook.brightness
        let step = glowStep
        ctx.saveGState()
        for i in 1...4 {
            let falloff = 1 - Double(i - 1) / 4
            let alpha = glow * CyberLook.glowCore * falloff * falloff
            ctx.setStrokeColor(CyberLook.cursor.withAlphaComponent(CGFloat(alpha)).cgColor)
            ctx.setLineWidth(step)
            ctx.stroke(c.insetBy(dx: -step * (CGFloat(i) - 0.5), dy: -step * (CGFloat(i) - 0.5)))
        }
        ctx.restoreGState()
        guard !reduced else { return }
        // Glyph rain inside the block, then the scanline, all translucent so
        // the character under the cursor still reads.
        ctx.saveGState()
        ctx.clip(to: c)
        let scroll = (t * CyberLook.cursorRain).truncatingRemainder(dividingBy: 1)
        let step0 = Int(floor(t * CyberLook.cursorRain))
        let g = c.height * CyberLook.glyphSize
        for k in -1...1 {
            let y = c.maxY - (CGFloat(k) + CGFloat(scroll)) * c.height - c.height * 0.5 - g / 2
            let seed = CyberGlyph.hash(step0 &- k, 5, Int(c.minX))
            let rect = CGRect(x: c.midX - g * 0.3, y: y, width: g * 0.6, height: g)
            CyberGlyph.draw(CyberGlyph.mask(seed: seed), in: rect, color: CyberLook.shade,
                            alpha: CGFloat(CyberLook.cursorShade + 0.25), context: ctx)
        }
        let phase = (t / CyberLook.scanPeriod).truncatingRemainder(dividingBy: 1)
        if phase < 0.3 {
            let y = c.maxY - c.height * CGFloat(phase / 0.3)
            ctx.setFillColor(CyberLook.rain.withAlphaComponent(0.8).cgColor)
            ctx.fill(CGRect(x: c.minX, y: y - 1, width: c.width, height: 2))
        }
        ctx.restoreGState()
    }

    private func drawDecode(_ d: Decode, _ now: Date, _ ctx: CGContext) {
        let r = CyberGeometry.cursorRect(col: d.col, row: d.row, cell: cell,
                                         viewHeight: bounds.height)
        // The letter has not been echoed yet, or is landing: hide the cell
        // behind a few random glyphs for DECODE_TIME.
        ctx.setFillColor(TerminalLook.backgroundNS.cgColor)
        ctx.fill(r)
        let left = d.end.timeIntervalSince(now)
        let total = d.end.timeIntervalSince(d.start)
        let fade = CGFloat(min(1, max(0, left / max(total, 0.001) * 1.4)))
        let frame = Int(now.timeIntervalSince(d.start) / 0.045)
        let g = r.insetBy(dx: r.width * 0.12, dy: r.height * 0.15)
        CyberGlyph.draw(CyberGlyph.mask(seed: CyberGlyph.hash(d.col, d.row, frame)), in: g,
                        color: CyberLook.rain, alpha: fade, context: ctx)
    }

    private func drawEpisode(_ e: CyberEpisode, _ now: Date, _ ctx: CGContext) {
        let g = cell.height * CyberLook.glyphSize
        for rc in e.rainCells(now) {
            let r = CyberGeometry.cursorRect(col: rc.col, row: rc.row, cell: cell,
                                             viewHeight: bounds.height)
            let box = CGRect(x: r.midX - g * 0.3, y: r.midY - g / 2, width: g * 0.6, height: g)
            ctx.setFillColor(TerminalLook.backgroundNS.withAlphaComponent(0.85).cgColor)
            ctx.fill(r)
            CyberGlyph.draw(rc.glyph, in: box,
                            color: rc.isHead ? NSColor.white : CyberLook.rain,
                            alpha: CGFloat(rc.brightness), context: ctx)
            if !rc.isHead {
                CyberGlyph.draw(rc.glyph, in: box, color: CyberLook.trailColor,
                                alpha: CGFloat(rc.brightness * 0.5), context: ctx)
            }
        }
        for sweep in e.sweeps(now) {
            for row in e.sweepRows {
                let r = rowRect(row)
                let x0 = CGFloat(sweep.x0) * cell.width
                let x1 = CGFloat(sweep.x1) * cell.width
                guard x1 > x0 else { continue }
                let bar = CGRect(x: x0, y: r.midY - 1.5, width: x1 - x0, height: 3)
                let colors = [CyberLook.rain.withAlphaComponent(0).cgColor,
                              CyberLook.rain.withAlphaComponent(0.95).cgColor] as CFArray
                guard let grad = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                                            colors: colors, locations: [0, 1]) else { continue }
                ctx.saveGState()
                ctx.clip(to: bar)
                let from = sweep.leadsRight ? CGPoint(x: x0, y: bar.midY) : CGPoint(x: x1, y: bar.midY)
                let to = sweep.leadsRight ? CGPoint(x: x1, y: bar.midY) : CGPoint(x: x0, y: bar.midY)
                ctx.drawLinearGradient(grad, start: from, end: to, options: [])
                ctx.restoreGState()
            }
        }
        let flare = e.flare(now)
        if flare > 0 {
            for row in e.sweepRows {
                let r = rowRect(row)
                let radius = cell.width * 6 * CGFloat(0.5 + flare)
                let center = CGPoint(x: CGFloat(e.cols) * cell.width / 2, y: r.midY)
                let colors = [CyberLook.rain.withAlphaComponent(CGFloat(flare)).cgColor,
                              CyberLook.rain.withAlphaComponent(0).cgColor] as CFArray
                guard let grad = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                                            colors: colors, locations: [0, 1]) else { continue }
                ctx.drawRadialGradient(grad, startCenter: center, startRadius: 0,
                                       endCenter: center, endRadius: radius, options: [])
            }
        }
    }
}
