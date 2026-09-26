"""Pins for the phone's "Needs you" tab and its one answer surface.

The iOS scheme (`BobPhoneTests`) owns projection and decode fixtures;
this file is the host-suite source pin: answer ownership, reach, the
shared count consumers, and the decision-list topology. A moved file or
an emptied pin must fail loudly.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
API = ROOT / "host" / "dark_army_daemon" / "api_server.py"
DAEMON = ROOT / "host" / "dark_army_daemon" / "daemon.py"
STATS = ROOT / "host" / "dark_army_daemon" / "session_stats.py"

NEEDS = PHONE / "NeedsYouView.swift"
ANSWER = PHONE / "AnswerBox.swift"
MODELS = PHONE / "Models.swift"
ACTIONS = PHONE / "Actions.swift"
APP = PHONE / "BobPhoneApp.swift"
DETAIL = PHONE / "AgentDetailView.swift"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"

#: The keys `session_stats.question_from_tool_input` builds the dict with.
QUESTION_KEYS = ("text", "options", "details", "header", "id", "multi_select")


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


@pytest.mark.parametrize("path", [NEEDS, ANSWER, MODELS, ACTIONS, APP, DETAIL,
                                  API, DAEMON, STATS, PBXPROJ])
def test_pin_files_exist(path):
    """A missing file is a failure, never a skip."""
    assert path.is_file(), f"missing file: {path}"


def test_the_tab_draws_the_projected_decision_list():
    """Short rows that route (20 Sep 2026): the tab draws its own compact
    row and opens the agent or card screen; it reuses neither the fleet's
    row nor the board's tile, and offers no answer of its own."""
    source = _read(NEEDS)
    assert "PhoneInbox.groups" in source
    assert "snapshot.decisionItems" in source
    assert "ForEach(groups)" in source
    assert "ForEach(group.items)" in source
    assert "struct InboxRow" in source
    assert "PhoneProcessRow" not in source
    assert "PhoneBoardCard" not in source
    assert "AnswerBox(" not in source
    assert "PhoneInboxRoute.open" in source
    assert "return .card(card)" in source
    assert "return .agent(agent, category)" in source
    assert "sheets.show(sheet)" in source
    assert "swipeActions(edge: .trailing" in source
    assert "allowsFullSwipe: false" in source
    assert "UserDefaults" not in source
    assert "UserDefaults" not in _read(PHONE / "Inbox.swift")


def test_the_tab_is_first_in_the_bar_and_badged():
    source = _read(APP)
    assert 'Label("Needs you"' in source
    assert ".badge(client.snapshot.needsYouCount)" in source
    assert "NeedsYouView(client: client)" in source
    # First: it must appear before the fleet tab in the same TabView. Matched
    # on the bare type name — the view took a `selection:` binding when a
    # resumed draft needed to bring its own tab forward.
    assert "TabView" in source
    body = source.split("TabView", 1)[1]
    assert body.index('Label("Needs you"') < body.index('Label("Fleet"')


def test_agent_question_decodes_every_key_the_daemon_publishes():
    stats = _read(STATS)
    source = _read(MODELS)
    block = source.split("struct AgentQuestion", 1)[1].split("\n}", 1)[0]
    for key in QUESTION_KEYS:
        assert f'"{key}"' in stats or f"'{key}'" in stats, (
            f"{key!r} is not built by session_stats.py — the phone must not "
            "invent fields the Mac does not publish")
        # A snake_case wire key is a camelCase Swift case with a raw value
        # (`case multiSelect = "multi_select"`); the plain keys match as is.
        assert re.search(rf"\bcase {key}\b", block) or f'= "{key}"' in block, (
            f"AgentQuestion does not decode {key!r}")
    # Tolerant decode is the house rule.
    assert "try c.decode(" not in block
    assert block.count("c.value(") == len(QUESTION_KEYS)


def test_the_option_buttons_are_gated_on_can_type():
    source = _read(ANSWER)
    # The same expression decides the buttons and reads `canType`.
    branch = re.search(r"if !options\.isEmpty \{\s*\n\s*if agent\.canType \{",
                       source)
    assert branch, "the option buttons are not gated on canType"
    # And the ungated half says why, rather than drawing a dead button.
    assert "optionCaptions" in source
    assert "cannot type into this terminal" in source


def test_the_answer_carries_all_three_fields_the_daemon_parses():
    source = _read(ANSWER)
    post = source.split("PhoneActions.answerQuestion", 1)[1].split("]", 1)[0]
    for field in ("session_id", "question_id", "option_index"):
        assert f'"{field}"' in post, f"{field} is not sent with the answer"
    # The question's own id, not an empty string: that match is what catches a
    # stale snapshot pointing the index at a different answer.
    assert "agent.question.id" in post
    # Zero-based, matching `0 <= option_index < len(options)` on the Mac.
    assert "String(index)" in post


def test_the_answer_is_never_arm_gated():
    source = _read(ANSWER)
    # The product name `Dark Army` contains `Arm`, and it is a label rather
    # than a gate — so it comes out before the check, which is otherwise the
    # one it always was.
    source = source.replace("Dark Army", "")
    assert "Arm" not in source, (
        "arming is for destructive verbs; a two-press answer is friction on "
        "the one path this exists to make fast")


def test_answer_box_is_the_only_file_that_posts_the_verb():
    files = sorted(PHONE.glob("*.swift"))
    assert files, "no Swift sources under ios/BobPhone"
    # The posting shape, not the bare token: `Client.swift` names the verb
    # in `answerHoldActions` (the answer hold, `test_phone_answer_hold.py`)
    # without ever posting it.
    posting = [p.name for p in files
               if "action: PhoneActions.answerQuestion" in p.read_text()]
    assert posting == ["AnswerBox.swift"], posting
    # And the detail screen no longer keeps a second copy of the reply shapes.
    detail = _read(DETAIL)
    assert "AnswerBox(agent:" in detail
    assert "private var replyBox" not in detail


def test_the_verb_is_a_chosen_phone_verb_at_both_ends():
    assert 'static let answerQuestion = "answer_question"' in _read(ACTIONS)
    api = _read(API)
    chosen = api.split("LAN_ACTIONS = (", 1)[1].split(")", 1)[0]
    assert '"answer_question"' in chosen, "the verb is not in LAN_ACTIONS"
    # And `_lan_run` has a branch for it that reaches the guarded coroutine.
    branch = api.split('if action == "answer_question":', 1)[1]
    assert "self._answer_question(" in branch.split("if action in (", 1)[0]


def test_the_daemon_s_guards_are_untouched():
    """The safety argument for this whole feature lives in `answer_question`."""
    source = _read(DAEMON)
    body = source.split("    async def answer_question(", 1)[1]
    body = body.split("\n    async def answer_permission(", 1)[0]
    assert "_prompts_by_session()" in body, "the permission-prompt refusal is gone"
    assert "_session_waiting_on_question" in body, "the waiting re-check is gone"
    assert "question_id != held_id" in body, "the question_id match is gone"
    assert "option_index < len(options)" in body, "the index bound is gone"


def test_the_new_views_are_registered_in_the_project():
    source = _read(PBXPROJ)
    for name in ("NeedsYouView.swift", "AnswerBox.swift", "Inbox.swift"):
        assert source.count(name) >= 2, f"{name} is not in the Xcode project"
        assert f"{name} in Sources" in source
        assert f"path = {name}" in source
    assert "PhoneInboxTests.swift in Sources" in source


def test_the_phone_choices_are_a_set_per_question():
    """TestFlight 33964826206: `choices` was typed `[Int: Int]` while
    `AnswerDrafts` stores `[Int: Set<Int>]`. The Mac panel compiles locally;
    the phone only archives in CI. Pin the type so a drift is a host-suite
    failure, not an archive."""
    source = _read(ANSWER)
    assert "private var choices: [Int: Set<Int>]" in source
    assert "private var choices: [Int: Int]" not in source


def test_a_chosen_toggle_tells_voiceover_it_is_selected():
    """The multi-question picks are drawn as ◉/○ glyphs — colour and shape
    only. VoiceOver needs the trait, or every option reads identically and
    the chosen one is unfindable."""
    source = _read(ANSWER)
    assert ".accessibilityAddTraits(" in source
    assert "choices[qi]?.contains(index) == true ? .isSelected : []" in source


# ── the blocked row says what it is blocked on ───────────────────────────────

def test_a_blocked_row_names_the_tool_and_what_it_wants_to_touch():
    """A bare "waiting" is what the incident's phone showed. The row's
    detail is the ask itself — the reducer reads the snapshot's own
    `permissions` list for this agent, never a second definition of
    blocked — and the list draws that detail in full."""
    inbox = _read(PHONE / "Inbox.swift")
    assert "promptsBySession[prompt.sessionId" in inbox
    assert "prompt.toolName" in inbox
    assert "prompt.detail" in inbox
    source = _read(NEEDS)
    assert "Text(item.detail)" in source
    assert "snapshot.permissions.filter" not in source


def test_the_verdict_stays_on_the_agent_screen():
    """One arm-then-confirm surface per verdict. Two would be two ways to
    half-answer the same question, and the agent screen already has it."""
    source = _read(NEEDS)
    for verb in ("Allow", "Deny", "permission_verdict", "pressPermission"):
        assert verb not in source, f"{verb} belongs on the agent screen"


def test_classification_lives_in_the_reducer_not_the_view():
    source = _read(NEEDS)
    assert "private var groups: [PhoneInboxGroup]" in source
    inbox = (PHONE / "Inbox.swift").read_text()
    assert "enum PhoneInbox" in inbox
    assert 'linkState == "ended"' not in inbox
    assert "manualSteps.isEmpty" not in inbox


def test_cards_read_the_published_flag_and_keep_the_empty_state_honest():
    source = _read(NEEDS)
    assert "snapshot.decisionItems.isEmpty" in source
    assert 'CommentLine(text: "nobody needs you")' in source
    assert "PhoneInboxRoute.open" in source
    # The list draws no tile and no note of the tile's: a card entry is the
    # same compact row a session entry is, and opens the card screen.
    assert "PhoneBoardCard" not in source
    assert "notice:" not in source
    assert 'PhoneSectionHeader(title: "CARDS WITHOUT A SESSION")' not in source
    inbox = _read(PHONE / "Inbox.swift")
    assert "if card.needsYou {" in inbox
    assert "} else if card.manualCheckDue {" in inbox


def test_the_list_moves_no_card_and_arms_nothing():
    """Mark done and Send back are the card screen's (20 Sep 2026): the
    list routes and does not answer, so it names neither board verb, keeps
    no arm and scopes no confirmation."""
    source = _read(NEEDS)
    for gone in ("PhoneActions.boardUpdate", "PhoneActions.boardReset",
                 '"column_name": "done"', "arm.confirm(", "arm.arm(",
                 "scope: card.id", "Arm()"):
        assert gone not in source, gone
    detail = _read(PHONE / "CardDetailView.swift")
    assert '["card_id": card.id, "column_name": "done"]' in detail
    assert "PhoneActions.boardReset" in detail


def test_both_counts_include_cards_and_decode_is_tolerant():
    source = _read(APP)
    assert ".badge(client.snapshot.needsYouCount)" in source
    assert "needsYouCount: client.snapshot.needsYouCount" in source
    assert "client.snapshot.agents.waiting.count" not in source
    models = _read(MODELS)
    assert 'case needsYou = "needs_you"' in models
    assert "needsYou = c.value(.needsYou, false)" in models
    assert "cards.filter(\\.needsYou)" in models
    assert "var needsYouCount: Int { decisionItems.count }" in models
    assert "agents.waiting.count + board.needsYouCards.count" not in models
    assert 'case closedBy = "closed_by"' in models
    assert 'case reviewedAt = "reviewed_at"' in models
    assert "closedBy = c.value(.closedBy, \"\")" in models
    assert "reviewedAt = c.maybe(.reviewedAt)" in models


def test_an_agent_row_offers_its_own_verbs_beside_dismiss():
    """23 Sep 2026: a finished agent is acknowledged and closed from Needs
    you, without opening its screen. The swipe carries the agent screen's
    own verbs, gated on the same `can_*` flags and sent with the same
    action and fields; Close is confirmed in a dialog, the rest sit under
    More, and every press queues under the row's own subject."""
    source = _read(NEEDS)
    detail = _read(DETAIL)
    verbs = source.split("enum InboxAgentVerb", 1)[1].split("\n}\n", 1)[0]
    offered = verbs.split("static func offered(for agent: Agent)", 1)[1].split("\n    }", 1)[0]
    gates = ["agent.canClose", "agent.canHide", "agent.canLowPriority", "agent.canStop"]
    assert [offered.index(g) for g in gates] == sorted(offered.index(g) for g in gates)
    for gate in gates:
        assert gate in detail, gate
    for action in ("closeTerminal", "hideSession", "lowPriority", "stopSession"):
        assert f"return PhoneActions.{action}" in verbs, action
    # Only a person's close finishes the card: the same assertion the agent
    # screen sends.
    assert '["session_id": session, "by_person": "1"]' in verbs
    assert '"by_person": "1"' in detail
    # One word list for both apps (`Verbs.swift`, 25 Sep 2026).
    assert "Verbs.closeTerminal.label" in verbs
    assert "Verbs.closeTerminal.label" in detail
    # Close is confirmed before it is sent; the swipe only opens the dialog.
    assert "onClose: { closing = item }" in source
    assert "Verbs.closeTerminal.noUndo" in source
    assert "press(.close, on: item)" in source
    mod = source.split("private struct InboxSwipeModifier", 1)[1].split("\n}\n", 1)[0]
    assert 'Label(Verbs.closeTerminal.label' in mod and 'Label("More"' in mod
    assert "accessibilityActions" in mod
    press = source.split("private func press(_ verb: InboxAgentVerb", 1)[1].split("\n    }\n", 1)[0]
    assert "client.enqueue(" in press and "scope: scope(item)" in press
    # Never the verdict, the answer or Delete from this list.
    assert "deleteAgent" not in source
    assert "permissionVerdict" not in source
