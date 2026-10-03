import Foundation

/// What one board tile reads from `BoardState`, as a value.
///
/// The tile used to observe the whole `BoardState` — thirty-odd published
/// slots — so a tick, an arm, a keystroke in the search, a drag passing over
/// another card or a card window opening redrew every tile on the board
/// (28 Sep 2026: "the app works slow on browsing, selecting"). `BoardView`
/// now works this out per card and hands it in; the tile holds the state
/// for its verbs only, never observed, and redraws when *its* facts differ.
///
/// Every field is about this card alone, except `selectingRow` and
/// `showsProjectChip`, which are row- and board-wide but change only on a
/// deliberate press.
struct BoardTileFacts: Equatable {
    var armed = false
    var armedHere = false
    var deleteArmed = false
    var doneArmed = false
    var starting = false
    var deleting = false
    /// Refine pressed and not yet on a snapshot (`BoardState.refining`).
    var refiningPressed = false
    var revealed = false
    var dropTargeted = false
    /// The daemon's words for this card's last refusal; empty for none.
    var refusal = ""
    /// The row in select mode, when it is this card's row; nil otherwise.
    var selectingRow: BoardColumn?
    var ticked = false
    /// Whether a click on the tick box or the title may change the tick
    /// (`RowSelection.admits`, or already ticked). False outside select mode.
    var tickAdmitted = false
    /// Whether the face names its project — `singleProject == nil`.
    var showsProjectChip = false

    @MainActor
    init(card: BoardCard, state: BoardState, chrome: BoardChrome) {
        let id = card.id
        armed = state.armed == id
        armedHere = state.armedHere == id
        deleteArmed = state.deleteArmed == id
        doneArmed = state.doneArmed == id
        starting = state.starting.contains(id)
        deleting = state.deleting.contains(id)
        refiningPressed = state.refining.contains(id)
        revealed = state.revealedCard == id
        if case .card(let target) = state.dropTarget { dropTargeted = target == id }
        refusal = state.refusals[id] ?? ""
        if let row = state.selectingRow, row.rawValue == card.column {
            selectingRow = row
            ticked = state.rowSelection.contains(id)
            tickAdmitted = ticked || RowSelection.admits(
                row, card: card, given: state.rowSelectionCards, chrome: chrome)
        }
        showsProjectChip = state.singleProject == nil
    }

    init() {}
}

/// One tile as the row's `ForEach` sees it: the card, its facts and the
/// board-wide chrome, so a change to any of them is a change to the data.
///
/// The lazy grid does not re-run its `ForEach` content when the data it was
/// handed compares equal — the content closure captures nothing it can see
/// moving, and the tile observes nothing. With the card alone as the data, a
/// press that changed only `BoardState` (an arm, a spinner, a refusal) built
/// fresh facts that no tile ever received: START stayed "START", and the
/// second press, read from that stale face, armed again instead of starting
/// (2 Oct 2026, `BoardScrollRedrawTests.testArmingStartRedrawsThatTile`).
struct BoardTileItem: Identifiable, Equatable {
    let card: BoardCard
    let facts: BoardTileFacts
    let chrome: BoardChrome
    var id: String { card.id }
}
