import XCTest
@testable import BobPanel

/// The pane's answer shape is one pure decision, pinned here so it cannot
/// drift from the phone's ladder in `ios/BobPhone/AnswerBox.swift`.
final class AnswerLayoutTests: XCTestCase {

    private func layout(hasOptions: Bool, questionCount: Int = 1,
                        multiSelect: Bool = false,
                        canType: Bool, promptUp: Bool = false,
                        stopped: Bool = true, channel: Bool = true)
        -> (options: StdoutPane.OptionShape, reply: Bool) {
        StdoutPane.answerLayout(hasOptions: hasOptions,
                                questionCount: questionCount,
                                multiSelect: multiSelect,
                                canType: canType,
                                promptUp: promptUp, stopped: stopped,
                                channel: channel)
    }

    /// Shape (a): buttons, and no second way to answer the one question.
    func testOptionsAndCanTypeGiveButtonsAndNoReply() {
        let l = layout(hasOptions: true, canType: true)
        XCTAssertEqual(l.options, .buttons)
        XCTAssertFalse(l.reply)
    }

    /// Shape (b): captions, and the free-text route stays because it is then
    /// the only one that works.
    func testOptionsWithoutCanTypeGiveCaptionsAndKeepReply() {
        let l = layout(hasOptions: true, canType: false)
        XCTAssertEqual(l.options, .captions)
        XCTAssertTrue(l.reply)
    }

    func testCaptionsWithoutChannelDropTheReply() {
        let l = layout(hasOptions: true, canType: false, channel: false)
        XCTAssertEqual(l.options, .captions)
        XCTAssertFalse(l.reply)
    }

    func testCaptionsWhileRunningDropTheReply() {
        let l = layout(hasOptions: true, canType: false, stopped: false)
        XCTAssertEqual(l.options, .captions)
        XCTAssertFalse(l.reply)
    }

    /// A live dialog is answerable regardless of the turn's state.
    func testButtonsAreNotGatedOnStopped() {
        let l = layout(hasOptions: true, canType: true, stopped: false)
        XCTAssertEqual(l.options, .buttons)
        XCTAssertFalse(l.reply)
    }

    /// The permission dialog owns the input line: nothing else is offered.
    func testPromptUpBeatsOptions() {
        for canType in [true, false] {
            let l = layout(hasOptions: true, canType: canType, promptUp: true)
            XCTAssertEqual(l.options, .none)
            XCTAssertFalse(l.reply)
        }
    }

    func testPromptUpAlsoSuppressesThePlainReply() {
        let l = layout(hasOptions: false, canType: false, promptUp: true)
        XCTAssertEqual(l.options, .none)
        XCTAssertFalse(l.reply)
    }

    /// No question: bit-for-bit the pane's old `ReplyBar` rule.
    func testNoOptionsReproducesTheOldReplyRule() {
        for stopped in [true, false] {
            for channel in [true, false] {
                let l = layout(hasOptions: false, canType: false,
                               stopped: stopped, channel: channel)
                XCTAssertEqual(l.options, .none)
                XCTAssertEqual(l.reply, stopped && channel,
                               "stopped=\(stopped) channel=\(channel)")
            }
        }
    }

    // MARK: - The count dimension

    /// One question keeps today's one-tap buttons; several become
    /// pick-one-per-question behind a single send, because the terminal's
    /// dialog advances on Enter and cannot take a half-answer.
    func testSeveralQuestionsWithCanTypeGiveTheBatchShape() {
        for count in 2...4 {
            let l = layout(hasOptions: true, questionCount: count,
                           canType: true)
            XCTAssertEqual(l.options, .batch, "count=\(count)")
            XCTAssertFalse(l.reply)
        }
    }

    func testOneQuestionKeepsTheOneTapButtons() {
        let l = layout(hasOptions: true, questionCount: 1, canType: true)
        XCTAssertEqual(l.options, .buttons)
        XCTAssertFalse(l.reply)
    }

    /// Without a typeable terminal the count changes nothing: captions, and
    /// the free-text route survives exactly as on one question.
    func testSeveralQuestionsWithoutCanTypeStayCaptions() {
        let l = layout(hasOptions: true, questionCount: 3, canType: false)
        XCTAssertEqual(l.options, .captions)
        XCTAssertTrue(l.reply)
    }

    /// The permission dialog still owns the input line, whatever the count.
    func testPromptUpBeatsTheBatchShape() {
        let l = layout(hasOptions: true, questionCount: 3, canType: true,
                       promptUp: true)
        XCTAssertEqual(l.options, StdoutPane.OptionShape.none)
        XCTAssertFalse(l.reply)
    }

    /// A live multi-question dialog is answerable regardless of the turn's
    /// state, exactly as the single-question buttons are.
    func testBatchIsNotGatedOnStopped() {
        let l = layout(hasOptions: true, questionCount: 2, canType: true,
                       stopped: false)
        XCTAssertEqual(l.options, .batch)
        XCTAssertFalse(l.reply)
    }

    // MARK: - The multi-select dimension

    /// One question that takes several answers cannot be one tap: it takes
    /// the pick-then-send shape even alone, so the boxes can be ticked
    /// before anything is typed.
    func testOneMultiSelectQuestionWithCanTypeGivesTheBatchShape() {
        let l = layout(hasOptions: true, questionCount: 1, multiSelect: true,
                       canType: true)
        XCTAssertEqual(l.options, .batch)
        XCTAssertFalse(l.reply)
    }

    /// Without a typeable terminal the flag changes nothing: captions, and
    /// the free-text route survives exactly as on a pick-one question.
    func testOneMultiSelectQuestionWithoutCanTypeStaysCaptions() {
        let l = layout(hasOptions: true, questionCount: 1, multiSelect: true,
                       canType: false)
        XCTAssertEqual(l.options, .captions)
        XCTAssertTrue(l.reply)
    }

    /// The permission dialog still owns the input line, whatever the shape.
    func testPromptUpBeatsMultiSelect() {
        let l = layout(hasOptions: true, questionCount: 1, multiSelect: true,
                       canType: true, promptUp: true)
        XCTAssertEqual(l.options, StdoutPane.OptionShape.none)
        XCTAssertFalse(l.reply)
    }

    /// A live multi-select dialog is answerable regardless of the turn's
    /// state, exactly as the other two pressable shapes are.
    func testMultiSelectBatchIsNotGatedOnStopped() {
        let l = layout(hasOptions: true, questionCount: 1, multiSelect: true,
                       canType: true, stopped: false)
        XCTAssertEqual(l.options, .batch)
        XCTAssertFalse(l.reply)
    }

    func testCannotTypeNoteWording() {
        let note = StdoutPane.cannotTypeNote
        XCTAssertFalse(note.isEmpty)
        XCTAssertTrue(note.lowercased().contains("terminal"))
        XCTAssertFalse(note.lowercased().contains("at the mac"))
    }
}

extension AnswerLayoutTests {
    func testCodexQuestionsAndOfferedRepliesNeverCreateSendRoute() throws {
        let data = Data(#"{"provider":"codex","question":{"text":"Proceed?","options":["Yes","No"]},"reply_options":["Accept","Iterate"],"interaction_note":"Answer in the original Codex session."}"#.utf8)
        let agent = try JSONDecoder().decode(Agent.self, from: data)
        let result = StdoutPane.answerLayout(hasOptions: !agent.question.options.isEmpty,
                                             canType: agent.canType, promptUp: false,
                                             stopped: true, channel: agent.channel)
        XCTAssertEqual(result.options, .captions)
        XCTAssertFalse(result.reply)
        XCTAssertEqual(agent.replyOptions, ["Accept", "Iterate"])
        XCTAssertFalse(StdoutPane.interactionExplanation(note: agent.interactionNote, captions: true).isEmpty)
    }
}
