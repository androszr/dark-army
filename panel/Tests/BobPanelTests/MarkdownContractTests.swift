import XCTest
@testable import BobPanel

/// The three pane rules `Markdown.swift` states, pinned: links are stripped
/// to their label, a single newline is not a line break, and a list item's
/// hanging indent is folded back into the item.
final class MarkdownContractTests: XCTestCase {

    /// A link's label stays; the destination — a capability an agent-written
    /// document could aim — goes.
    func testLinksAreStrippedToTheirLabel() {
        let inline = Markdown.inline("see [the plan](https://evil.example/x)")
        XCTAssertEqual(String(inline.characters), "see the plan")
        for run in inline.runs {
            XCTAssertNil(run.link, "no live .link may survive the strip")
        }
    }

    /// Two source lines with no blank line between them are one paragraph —
    /// a single newline is not a line break.
    func testASingleNewlineIsNotALineBreak() {
        let blocks = Markdown.blocks("one line\nand its continuation")
        XCTAssertEqual(blocks.count, 1)
        guard case .paragraph = blocks[0].kind else {
            return XCTFail("expected a paragraph, got \(blocks[0].kind)")
        }
        XCTAssertEqual(blocks[0].text, "one line and its continuation")
        // A blank line, by contrast, does split.
        XCTAssertEqual(Markdown.blocks("one\n\ntwo").count, 2)
    }

    /// A plain line indented under an open list item is the second half of
    /// the sentence the marker started, not a new block.
    func testAHangingIndentIsFoldedBackIntoTheItem() {
        let blocks = Markdown.blocks("- first half of the item\n  and the rest of it")
        XCTAssertEqual(blocks.count, 1)
        guard case .bullet(let depth, let marker) = blocks[0].kind else {
            return XCTFail("expected a bullet, got \(blocks[0].kind)")
        }
        XCTAssertEqual(depth, 0)
        XCTAssertEqual(marker, "•")
        XCTAssertEqual(blocks[0].text, "first half of the item and the rest of it")
    }

    /// A `|` table is cells, not pipes: the alignment row goes, every cell is
    /// trimmed, and a ragged row is padded to the widest so a grid can draw it.
    func testATableIsSplitIntoCellsWithoutItsAlignmentRow() {
        let blocks = Markdown.blocks("| Commit | TestFlight |\n|---|:---:|\n| `6a39fdb` | success |\n| short |")
        XCTAssertEqual(blocks.count, 1)
        guard case .table = blocks[0].kind else {
            return XCTFail("expected a table, got \(blocks[0].kind)")
        }
        XCTAssertEqual(Markdown.tableRows(blocks[0].text), [
            ["Commit", "TestFlight"],
            ["`6a39fdb`", "success"],
            ["short", ""],
        ])
        // A cell is inline Markdown: the backticks are syntax, not ink.
        XCTAssertEqual(String(Markdown.inline("`6a39fdb`").characters), "6a39fdb")
    }

    /// A code span gets a ground, so it still reads as code inside a pane
    /// that is monospaced already.
    func testACodeSpanIsTinted() {
        let inline = Markdown.inline("run `HEAD` now")
        let tinted = inline.runs.filter { $0.backgroundColor != nil }
        XCTAssertEqual(tinted.count, 1)
        XCTAssertEqual(String(inline[tinted[0].range].characters), "HEAD")
    }

    /// An ASCII CRT is `|` ink, not a table: the bezel, the stand and any
    /// interior pipes stay in one preformatted block. Wrapping those lines
    /// as cells is what made the phone drawings stop looking like screens.
    func testACrtBannerIsPreformattedNotATable() {
        let source = """
          .---------------------------------------------------.
          |  [fsociety // finance-demo]        [_][=][X]      |
          |  > ./ship.sh --target=finance-demo | no float     |
          '----------------------------------------------------'
                                |||
                       --------_|_--------
        """
        let blocks = Markdown.blocks(source)
        XCTAssertEqual(blocks.count, 1, "the drawing is one block, not a table plus leftover lines")
        guard case .code = blocks[0].kind else {
            return XCTFail("expected preformatted, got \(blocks[0].kind)")
        }
        XCTAssertTrue(blocks[0].text.contains("|  [fsociety // finance-demo]"),
                      "the left bezel stays ink")
        XCTAssertTrue(blocks[0].text.contains("[_][=][X]      |"),
                      "the right bezel stays ink")
        XCTAssertTrue(blocks[0].text.contains("|  > ./ship.sh --target=finance-demo | no float     |"),
                      "an interior pipe is still the drawing, not a cell break")
        XCTAssertTrue(blocks[0].text.contains("|||"))
        XCTAssertTrue(blocks[0].text.contains("--------_|_--------"))
        XCTAssertFalse(blocks[0].text.contains("\n\n"),
                       "the stand is part of the same drawing, not a second block")
    }

    /// This repo's own banners use the two-pipe plinth `_________|_|_________`.
    func testTheUnderscorePlinthStaysOnTheDrawing() {
        let source = """
          .--------.
          |  hi    |
          '--------'
                            |||
                   _________|_|_________
        """
        let blocks = Markdown.blocks(source)
        XCTAssertEqual(blocks.count, 1)
        guard case .code = blocks[0].kind else {
            return XCTFail("expected preformatted, got \(blocks[0].kind)")
        }
        XCTAssertTrue(blocks[0].text.contains("|||"))
        XCTAssertTrue(blocks[0].text.contains("_________|_|_________"))
    }

    /// A heading, a CRT, and the caption under it are three blocks — the
    /// drawing must not swallow the prose around it.
    func testACrtBannerDoesNotSwallowTheCaption() {
        let source = """
        SHIP | Phase 0 | FINANCE-DEMO

          .--------.
          |  hi    |
          '--------'
                            |||
                       ----_|_----
        Agent /ship - Pipeline
        """
        let blocks = Markdown.blocks(source)
        XCTAssertEqual(blocks.count, 3)
        guard case .paragraph(let indent) = blocks[0].kind else {
            return XCTFail("expected the heading as a paragraph, got \(blocks[0].kind)")
        }
        XCTAssertEqual(indent, 0)
        XCTAssertEqual(blocks[0].text, "SHIP | Phase 0 | FINANCE-DEMO")
        guard case .code = blocks[1].kind else {
            return XCTFail("expected the drawing as preformatted, got \(blocks[1].kind)")
        }
        XCTAssertTrue(blocks[1].text.contains("|  hi    |"))
        XCTAssertTrue(blocks[1].text.contains("----_|_----"))
        guard case .paragraph = blocks[2].kind else {
            return XCTFail("expected the caption as a paragraph, got \(blocks[2].kind)")
        }
        XCTAssertEqual(blocks[2].text, "Agent /ship - Pipeline")
    }

    /// The re-parse gate: `MarkdownText` is equal exactly on its three
    /// inputs, which is what `.equatable()` at the call sites compares.
    func testMarkdownTextEquatableOnItsInputs() {
        XCTAssertEqual(MarkdownText(source: "a", base: 11, mono: true),
                       MarkdownText(source: "a", base: 11, mono: true))
        XCTAssertNotEqual(MarkdownText(source: "a", base: 11, mono: true),
                          MarkdownText(source: "b", base: 11, mono: true))
        XCTAssertNotEqual(MarkdownText(source: "a", base: 11, mono: true),
                          MarkdownText(source: "a", base: 12, mono: true))
        XCTAssertNotEqual(MarkdownText(source: "a", base: 11, mono: true),
                          MarkdownText(source: "a", base: 11, mono: false))
    }
}
