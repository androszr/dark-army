import XCTest
@testable import BobPanel

/// Prepare's opinion about which project a card belongs to, decided once in a
/// pure static so the composer has no logic of its own to test.
final class ProjectSuggestionTests: XCTestCase {

    private func project(_ name: String, _ root: String) -> BoardProject {
        var p = BoardProject()
        p.name = name
        p.root = root
        return p
    }

    private var projects: [BoardProject] {
        [project("dark-army", "/Users/x/Code/dark-army"),
         project("shop", "/Users/x/Code/shop")]
    }

    /// No key from an older daemon, or a daemon with no opinion, both arrive
    /// as empty — and empty must say nothing rather than blank the picker.
    func testAnEmptyRootIsNoOffer() {
        XCTAssertNil(ProjectSuggestion.offer(
            root: "", current: "/Users/x/Code/shop", projects: projects))
        XCTAssertNil(ProjectSuggestion.offer(
            root: "   ", current: "", projects: projects))
    }

    /// The second, independent guard: a root this panel's snapshot cannot
    /// list is a root its picker cannot select, so offering it would leave
    /// the menu showing nothing.
    func testARootNoListedProjectMatchesIsNoOffer() {
        XCTAssertNil(ProjectSuggestion.offer(
            root: "/Users/x/Code/somewhere-else", current: "",
            projects: projects))
        XCTAssertNil(ProjectSuggestion.offer(
            root: "/Users/x/Code/shop", current: "", projects: []))
    }

    /// Suggesting the folder the draft already has is a line with no work
    /// behind it.
    func testTheRootAlreadySelectedIsNoOffer() {
        XCTAssertNil(ProjectSuggestion.offer(
            root: "/Users/x/Code/shop", current: "/Users/x/Code/shop",
            projects: projects))
    }

    /// A real match hands back the listed project, so the row can name it the
    /// way the picker does.
    func testARealMatchIsOfferedByName() {
        let offer = ProjectSuggestion.offer(
            root: "/Users/x/Code/dark-army",
            current: "/Users/x/Code/shop", projects: projects)
        XCTAssertEqual(offer?.name, "dark-army")
        XCTAssertEqual(offer?.root, "/Users/x/Code/dark-army")
    }

    // MARK: - decide: apply, and offer a way back

    /// `decide` is `nil` exactly where `offer` is: an empty root, a root no
    /// listed project matches, and the root already selected all move
    /// nothing and draw nothing.
    func testDecideIsNilWhereOfferIsNil() {
        XCTAssertNil(ProjectSuggestion.decide(
            root: "", current: "/Users/x/Code/shop", projects: projects))
        XCTAssertNil(ProjectSuggestion.decide(
            root: "/Users/x/Code/somewhere-else", current: "/Users/x/Code/shop",
            projects: projects))
        XCTAssertNil(ProjectSuggestion.decide(
            root: "/Users/x/Code/shop", current: "/Users/x/Code/shop",
            projects: projects))
    }

    /// A real match with a listed current project: the picker moves to the
    /// match and the button names the project it held.
    func testARealMatchAppliesAndOffersThePreviousProjectByName() {
        let d = ProjectSuggestion.decide(
            root: "/Users/x/Code/dark-army",
            current: "/Users/x/Code/shop", projects: projects)
        XCTAssertEqual(d?.apply.name, "dark-army")
        XCTAssertEqual(d?.apply.root, "/Users/x/Code/dark-army")
        XCTAssertEqual(d?.revertTo?.name, "shop")
        XCTAssertEqual(d?.revertTo?.root, "/Users/x/Code/shop")
    }

    /// The picker's `—`: nothing was chosen, so there is nothing to go back
    /// to, and the suggestion is applied with no line under it.
    func testAnEmptyCurrentAppliesWithNothingToRevertTo() {
        let d = ProjectSuggestion.decide(
            root: "/Users/x/Code/dark-army", current: "",
            projects: projects)
        XCTAssertNotNil(d)
        XCTAssertEqual(d?.apply.name, "dark-army")
        XCTAssertNil(d?.revertTo)
        let padded = ProjectSuggestion.decide(
            root: "/Users/x/Code/dark-army", current: "   ",
            projects: projects)
        XCTAssertNil(padded?.revertTo)
    }

    /// A current root the picker cannot show (a window closed since) has no
    /// name to put on a button, so the suggestion is applied and no way back
    /// is offered.
    func testAnUnlistedCurrentAppliesWithNothingToRevertTo() {
        let d = ProjectSuggestion.decide(
            root: "/Users/x/Code/dark-army",
            current: "/Users/x/Code/gone", projects: projects)
        XCTAssertEqual(d?.apply.root, "/Users/x/Code/dark-army")
        XCTAssertNil(d?.revertTo)
    }
}
