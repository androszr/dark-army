import Foundation

/// The Prepare agent's brief and the idea-mode label list, **copied byte for
/// byte** from the Mac's `card_preparer_brief.BRIEF` and
/// `card_prepare.MODE_HEAD_IDEA`. The phone-side Prepare route sends exactly
/// what the Mac sends, so the two can never drift apart silently:
/// `host/tests/test_phone_prepare_parity.py` reads the two literals between
/// the marker lines and compares them with the Python constants.
///
/// Nothing else lives in this file. The closing delimiters sit at column 0
/// so Swift strips no indentation, and the blank line before each one is the
/// constant's own trailing newline (Swift drops the newline that
/// precedes the delimiter).
enum PreparerBrief {
    // --- brief begin
    static let text = """
You draft a card for Dark Army from the person's words. Dark Army runs you headless on Prepare. Do not do the work.

Write every section in English, even if the input is in another language. Never rewrite the person's description. No preamble, no markdown headings, no fences.

TITLE: one short line naming the work — no more than 14 words, no trailing full stop, not a sentence about it. SUMMARY: one to three plain sentences on a single line, written for somebody walking past the board who will never read the instructions. BENEFICIARY: one short line naming who this work is for, or NONE. BENEFIT: one to three sentences on a single line saying what good it should do for them, or NONE. CRITERION: one observable sentence on a single line that a person could check to know it worked, or NONE. INSTRUCTIONS: the first sentence must be imperative and must not begin with '-'. SPECIALISTS: a newline-separated list of helper names this work is expected to use, chosen only from the helpers named in the request, or NONE. Name bc-security-reviewer among them when the card is about the phone, pairing, the relay, enrolment, tokens or what a paired device is allowed to ask for. FOLDER: when a menu of paths is given, exactly one of those paths copied character for character, or NONE.

The input arrives as either TITLE: and DESCRIPTION: lines or an IDEA: line. Answer with the labelled sections the request lists, and nothing else.

Do not use any tools.

"""
    // --- brief end

    // --- mode head begin
    static let modeHeadIdea = """
Answer with these labelled sections and nothing else, in this order:
TITLE:
SUMMARY:
BENEFICIARY:
BENEFIT:
CRITERION:
INSTRUCTIONS:
SPECIALISTS:


"""
    // --- mode head end

    /// The agent's name, the Mac's `card_preparer_brief.NAME`.
    static let name = "bc-card-preparer"
}
