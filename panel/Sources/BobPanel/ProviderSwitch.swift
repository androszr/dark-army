import SwiftUI

/// Who takes this card, as a row of brand marks you click — or land on with
/// the keyboard — rather than a word with a menu behind it.
///
/// The old control was a chip labelled `claude · opus-5` opening a `Menu` — two
/// gestures to change one field that is, on this board, the field deciding
/// whether a card can start at all. A mark row makes it one press, and the same
/// row is drawn in the card's detail window so the choice looks identical in
/// both places.
///
/// **The artwork is `ProviderMark`'s**, unchanged and not copied: the marks the
/// process table already draws, so a logo means the same thing everywhere in
/// the app. That reuse carries one trap, which is why `ProviderChoice.knownMarks`
/// exists — `ProviderMark.image(for:)`'s `default:` arm returns *Claude's*
/// mark, so an unknown provider name would silently wear the wrong company's
/// logo. Every mark drawn here is gated on the known set first; an assistant
/// outside it is drawn as its **name** on a tile in the same row, never as
/// somebody else's logo and never as a menu.
///
/// **The row is one keyboard stop.** The group is `.focusable`, a roving
/// `cursor` moves along it on ← / →, Space or Return chooses the tile under the
/// cursor and Escape lets go. The system focus ring is disabled because the
/// CRT draws its own: a 1pt `Theme.phosphor` border on the cursor tile while
/// the group holds focus — colour and shape, never a tint alone. The panel's
/// key monitor stands aside while the row has focus (`KeyRouter.controlFocused`,
/// reported through `onFocusChange`), or it would eat the arrows first.
///
/// **VoiceOver hears one group** — a contained accessibility element
/// named `ProviderChoice.groupLabel` whose value is the chosen assistant or
/// "not chosen" — and inside it one button per tile, by name, the chosen one
/// `.isSelected`. The phone's `PhoneProviderSwitch` speaks the same words off
/// the same byte-pinned rule.
struct ProviderSwitch: View {
    /// The assistants the daemon publishes (`board.tools`), in the order it
    /// published them. Never re-sorted: the surface reads the daemon's list and
    /// re-derives nothing about it.
    let tools: [String]
    var installed: [String: Bool] = [:]
    /// The card's current assistant; `""` is "nobody yet".
    let selected: String
    /// A record of what ran draws as one mark and answers no press — and takes
    /// no focus.
    var interactive: Bool = true
    /// The route back to unassigned. Only the sheet gets it: unassigning also
    /// costs the model choice (the store clears it on retool), which is a
    /// deliberate act and not something a stray click on a 260pt card face
    /// should be able to do.
    var showsNobody: Bool = false
    var pick: (String) -> Void = { _ in }
    /// Told `true` when the group takes keyboard focus and `false` when it
    /// lets go — the caller's route to `KeyRouter.controlFocused`. Fired
    /// `false` on disappear too, so a card rebuilt under a focused row can
    /// never leave the monitor standing aside for nothing.
    var onFocusChange: (Bool) -> Void = { _ in }

    /// The three the artwork actually has, forwarded from the shared rule so
    /// `recordMark` and the rail's own reader keep one answer.
    static let knownMarks: Set<String> = ProviderChoice.knownMarks

    static let markSize: CGFloat = 10
    static let target: CGFloat = 16
    static let spacing: CGFloat = 3
    static let underline: CGFloat = 1

    /// The single mark a bound card wears as its record of what ran, or `nil`
    /// where there is nothing honest to draw.
    static func recordMark(selected: String) -> String? {
        knownMarks.contains(selected) ? selected : nil
    }

    private static func name(for provider: String) -> String {
        ProviderMark.displayName(provider)
    }

    @State private var cursor: Int = 0
    @FocusState private var focused: Bool

    private var tiles: [ProviderChoice.Tile] {
        ProviderChoice.tiles(tools: tools, installed: installed,
                             selected: selected, showsNobody: showsNobody)
    }

    var body: some View {
        let tiles = self.tiles
        HStack(spacing: Self.spacing) {
            ForEach(Array(tiles.enumerated()), id: \.element.provider) { index, entry in
                tile(entry, index: index)
            }
        }
        .focusable(interactive, interactions: .activate)
        .focusEffectDisabled()
        .focused($focused)
        .onKeyPress(.leftArrow) { move(-1, count: tiles.count) }
        .onKeyPress(.rightArrow) { move(1, count: tiles.count) }
        .onKeyPress(.space) { choose(tiles) }
        .onKeyPress(.return) { choose(tiles) }
        .onKeyPress(.escape) {
            guard focused && interactive else { return .ignored }
            focused = false
            return .handled
        }
        .onAppear { cursor = ProviderChoice.startCursor(tiles: tiles) }
        .onChange(of: selected) { _, _ in
            cursor = ProviderChoice.startCursor(tiles: self.tiles)
        }
        .onChange(of: focused) { _, now in onFocusChange(now) }
        .onDisappear { onFocusChange(false) }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(ProviderChoice.groupLabel)
        .accessibilityValue(ProviderChoice.value(selected: selected,
                                                 name: ProviderMark.displayName))
    }

    private func move(_ step: Int, count: Int) -> KeyPress.Result {
        guard focused && interactive else { return .ignored }
        cursor = ProviderChoice.moved(cursor: cursor, count: count, step: step)
        return .handled
    }

    private func choose(_ tiles: [ProviderChoice.Tile]) -> KeyPress.Result {
        guard focused && interactive else { return .ignored }
        guard tiles.indices.contains(cursor) else { return .handled }
        let entry = tiles[cursor]
        // A press on the tile already chosen is a no-op rather than a toggle:
        // unassigning is not what somebody who slipped meant, and it would
        // take the model choice with it.
        if entry.installed && !entry.selected { pick(entry.provider) }
        return .handled
    }

    /// The CRT's own focus ring: drawn on the cursor tile while the group
    /// holds focus, and on nothing otherwise.
    private func ring(_ index: Int) -> some View {
        Rectangle()
            .strokeBorder(focused && cursor == index ? Theme.phosphor : Color.clear,
                          lineWidth: 1)
    }

    @ViewBuilder
    private func tile(_ entry: ProviderChoice.Tile, index: Int) -> some View {
        if entry.provider.isEmpty {
            nobodyButton(index: index)
        } else {
            markButton(entry, index: index)
        }
    }

    @ViewBuilder
    private func markButton(_ entry: ProviderChoice.Tile, index: Int) -> some View {
        Button {
            // A press on the mark already chosen is a no-op rather than a
            // toggle: unassigning is not what somebody who slipped meant, and
            // it would take the model choice with it.
            if interactive && entry.installed && !entry.selected { pick(entry.provider) }
        } label: {
            VStack(spacing: 1) {
                if entry.hasMark {
                    ProviderMark(provider: entry.provider,
                                 size: Self.markSize,
                                 tint: entry.selected ? Theme.phosphorBright : Theme.faint)
                } else {
                    // An assistant Dark Army offers but has no logo for wears
                    // its name on the same tile, in the same row — never
                    // somebody else's mark, and never a menu.
                    Text(Self.name(for: entry.provider))
                        .font(Theme.mono(10))
                        .foregroundStyle(entry.selected ? Theme.phosphorBright : Theme.faint)
                        .fixedSize()
                }
                if !entry.installed {
                    Text(ProviderChoice.missingNote)
                        .font(Theme.mono(8))
                        .foregroundStyle(Theme.faint)
                }
                // Reserved under every mark, so choosing another one moves
                // nothing. Selection is never colour-alone.
                Rectangle()
                    .fill(entry.selected ? Theme.phosphor : Color.clear)
                    .frame(height: Self.underline)
            }
            .frame(minWidth: Self.target, minHeight: Self.target)
            .frame(height: Self.target)
            .contentShape(Rectangle())
            .overlay(ring(index))
        }
        .buttonStyle(.plain)
        .focusable(false)
        .disabled(!interactive)
        .opacity(entry.installed ? 1 : 0.45)
        .clickable()
        .help(entry.installed ? helpText(entry.provider) : ProviderChoice.missingNote)
        .accessibilityLabel(entry.installed ? Self.name(for: entry.provider)
                            : "\(Self.name(for: entry.provider)), \(ProviderChoice.missingNote)")
        .accessibilityAddTraits(entry.selected ? [.isSelected] : [])
    }

    private func helpText(_ provider: String) -> String {
        interactive ? "Run with \(Self.name(for: provider))"
                    : Self.name(for: provider)
    }

    @ViewBuilder
    private func nobodyButton(index: Int) -> some View {
        Button {
            if interactive && !selected.isEmpty { pick("") }
        } label: {
            Text(ProviderChoice.nobody)
                .font(Theme.mono(10))
                .foregroundStyle(selected.isEmpty ? Theme.phosphorBright : Theme.faint)
                .padding(.horizontal, 4)
                .padding(.vertical, 2)
                .overlay(Rectangle().strokeBorder(
                    selected.isEmpty ? Theme.phosphor : Theme.hair, lineWidth: 1))
                .contentShape(Rectangle())
                .padding(1)
                .overlay(ring(index))
        }
        .buttonStyle(.plain)
        .focusable(false)
        .disabled(!interactive)
        .clickable()
        .help("Leave the assistant unchosen")
        .accessibilityLabel(ProviderChoice.nobody)
        .accessibilityAddTraits(selected.isEmpty ? [.isSelected] : [])
    }
}

/// The switcher's rule, pure and shared: which tiles the row draws, what the
/// group says it is worth, and where the keyboard cursor may go.
///
/// **Byte-equal on the phone** (`ios/BobPhone/ComposerView.swift`, pinned by
/// `host/tests/test_provider_switcher.py`), which is why nothing in here names
/// `ProviderMark`, `PhoneProviderMark`, `Theme` or any other surface type: the
/// spoken name arrives through the `name:` closure.
enum ProviderChoice {
    /// What a screen reader calls the group.
    static let groupLabel = "Assistant"
    /// The group's value while no assistant is chosen.
    static let unchosen = "not chosen"
    /// The one tile that is a route back to unassigned, drawn by the card
    /// window alone.
    static let nobody = "Nobody yet"
    static let missingNote = "not installed on this Mac"
    /// The three the artwork actually has. A fourth entry in
    /// `dispatch._EXECUTABLES` draws as its name on a tile rather than
    /// borrowing somebody else's logo for it.
    static let knownMarks: Set<String> = ["claude", "codex", "grok"]

    /// One entry of the row. `provider` is `""` for the "Nobody yet" tile.
    struct Tile: Equatable {
        let provider: String
        let hasMark: Bool
        let selected: Bool
        let installed: Bool
    }

    /// One tile per offered tool, in the daemon's order, then the "Nobody
    /// yet" tile where the caller offers the route back to unassigned.
    static func tiles(tools: [String], installed: [String: Bool] = [:],
                      selected: String, showsNobody: Bool) -> [Tile] {
        var out = tools.map {
            Tile(provider: $0,
                 hasMark: knownMarks.contains($0),
                 selected: !selected.isEmpty && $0 == selected,
                 installed: installed[$0] ?? true)
        }
        if showsNobody {
            out.append(Tile(provider: "", hasMark: false, selected: selected.isEmpty,
                            installed: true))
        }
        return out
    }

    /// True only where the row cannot tell the truth and the caller draws an
    /// inert word chip instead: nothing offered (an older daemon, or dispatch
    /// switched off), or a chosen assistant that is not among the offered ones
    /// — a selection the row could only render by leaving every tile dim,
    /// which reads as *unassigned* and is a lie about a card that names
    /// somebody. An assistant with no artwork is **not** a reason: it draws
    /// as its name.
    static func wordChipOnly(tools: [String], selected: String) -> Bool {
        if tools.isEmpty { return true }
        if !selected.isEmpty && !tools.contains(selected) { return true }
        return false
    }

    /// The group's spoken value: the chosen assistant's name, or `unchosen`.
    static func value(selected: String, name: (String) -> String) -> String {
        selected.isEmpty ? unchosen : name(selected)
    }

    /// The cursor after one arrow press. Clamped, never wrapped: a macOS
    /// radio group stops at its ends.
    static func moved(cursor: Int, count: Int, step: Int) -> Int {
        guard count > 0 else { return 0 }
        return min(max(cursor + step, 0), count - 1)
    }

    /// Where the cursor starts: on the chosen tile, else the first.
    static func startCursor(tiles: [Tile]) -> Int {
        tiles.firstIndex(where: \.selected) ?? 0
    }
}
