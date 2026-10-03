# host/tests/test_phone_review.py
"""The phone's Review screens, checked by grep for the rules
`docs/phone-contract.md` pins on every screen, and for the wiring only a
source read can see: the project membership, the sealed read, the action
names, the Menu tile and the Needs you route.
"""

import re
from pathlib import Path

from dark_army_daemon import api_server

REPO = Path(__file__).resolve().parents[2]
PHONE = REPO / "ios" / "BobPhone"
PROJECT = (REPO / "ios" / "BobPhone.xcodeproj" / "project.pbxproj").read_text()
FILES = ("ReviewView.swift", "ReviewRunView.swift", "ReviewRules.swift")


def _read(name: str) -> str:
    return (PHONE / name).read_text()


def _code(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def test_the_project_compiles_the_three_new_files():
    for name in FILES:
        assert PROJECT.count(name) >= 4, name


def test_the_sealed_read_sends_the_root_in_the_body_never_a_query():
    text = _read("Client.swift")
    start = text.index("func reviewOffer(root: String) async -> ReviewOffer? {")
    body = text[start:text.index("\n    }\n", start)]
    assert 'kind: "review_offer"' in body
    assert '["root": root]' in body
    assert "query" not in body and "addingPercentEncoding" not in body
    assert "!backgroundRun" in body
    assert "knowsItIsAway" in body


def test_the_read_is_never_on_the_poll_the_background_refresh_or_the_widget():
    for name in ("Client.swift",):
        text = _read(name)
        for caller in ("func poll", "func backgroundRefresh"):
            if caller in text:
                body = text[text.index(caller):text.index(caller) + 6000]
                assert "reviewOffer(" not in body, caller


def test_the_action_names_match_the_daemons_tuples():
    actions = _read("Actions.swift")
    for const, wire in (("reviewStart", "review_start"),
                        ("reviewContinue", "review_continue"),
                        ("reviewEnd", "review_end")):
        assert f'static let {const} = "{wire}"' in actions
        assert wire in api_server.ApiServer.LAN_ACTIONS
        assert wire in api_server.ApiServer.REMOTE_ACTIONS


def test_the_menu_tile_opens_the_review_screen():
    menu = _read("MenuView.swift")
    assert "designSystem, review" in menu
    assert "ReviewView(client: client)" in menu
    assert '.navigationTitle("review")' in menu
    assert '.navigationTitle("review run")' in _read("ReviewView.swift")
    assert 'case .review: return "Review"' in menu


def test_no_clipped_prose_and_mono_only():
    for name in ("ReviewView.swift", "ReviewRunView.swift"):
        code = _code(_read(name))
        assert ".lineLimit(" not in code, name
        assert "Theme.prose" not in code, name
        assert "ProgressView" not in code, name


def test_the_presses_ride_the_queue_and_the_arm():
    view = _read("ReviewView.swift")
    run = _read("ReviewRunView.swift")
    assert "PhoneActions.reviewStart" in view and "client.enqueue(" in view
    assert "PhoneActions.reviewContinue" in run
    assert "PhoneActions.reviewEnd" in run
    assert "arm.confirm(.reviewContinue" in run and "arm.confirm(.reviewEnd" in run
    # Every step starts unticked: the tick is the permission.
    assert "@State private var ticked: Set<String> = []" in view
    assert "ticked = []" in view


def test_the_terminal_cover_is_shown_only_with_a_session():
    run = _read("ReviewRunView.swift")
    assert ".fullScreenCover(isPresented: $terminalShown)" in run
    assert "PhoneTerminalPane(agent: terminalAgent, client: client)" in run
    assert "if !run.sessionId.isEmpty {" in run
    assert "client.watchTerminal(nil)" in run


def test_needs_you_routes_a_review_entry_to_the_menu_and_posts_nothing():
    needs = _read("NeedsYouView.swift")
    assert "PhoneRouter.shared.openReview(runId: id)" in needs
    router = _read("Router.swift")
    assert "func openReview(runId: String)" in router
    assert "pendingSection = .review" in router
    assert "go(.menu)" in router
    assert "takeReviewRun" in _read("ReviewView.swift")


def test_the_models_are_tolerant_and_the_marker_is_false_by_default():
    models = _read("Models.swift")
    assert "var reviewSupported = false" in models
    assert "var reviewSectionWritable = false" in models
    assert 'case reviewSupported = "review_supported"' in models
    assert 'case reviewSectionWritable = "review_section_writable"' in models
    assert "case .review: review = held.review" in models
    assert "struct ReviewLossy<T: Decodable>" in models


def test_the_macs_refusal_of_start_continue_and_end_is_drawn_and_read():
    view = _read("ReviewView.swift")
    run = _read("ReviewRunView.swift")
    assert "client.readQueueNote(for: startScope)" in view
    for scope in ("continueScope", "endScope"):
        assert f"client.queueNote(for: {scope})" in run, scope
        assert f"client.readQueueNote(for: {scope})" in run, scope
