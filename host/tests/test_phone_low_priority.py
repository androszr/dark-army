"""Source-contract pins over `ios/BobPhone` for the Low priority button.

The phone's half of `low_priority_session` cannot be compiled here, so these
read the Swift as text: the action name, the decode keys, the button's gate,
and that the verb never joined `settlingActions` (which would fade a row that
is not leaving).
"""
from pathlib import Path

PHONE = Path(__file__).resolve().parents[2] / "ios" / "BobPhone"
PANEL = Path(__file__).resolve().parents[2] / "panel" / "Sources" / "BobPanel"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _body(text: str, marker: str) -> str:
    start = text.find(marker)
    assert start >= 0, f"{marker!r} not found"
    return text[start:text.find("]", start)]


def test_the_action_name_is_the_daemons():
    actions = _read(PHONE / "Actions.swift")
    assert 'static let lowPriority = "low_priority"' in actions
    assert "low_priority_session" in actions


def test_the_phone_agent_decodes_the_reach_flag_with_a_false_default():
    models = _read(PHONE / "Models.swift")
    assert 'case canLowPriority = "can_low_priority"' in models
    assert "canLowPriority = c.value(.canLowPriority, false)" in models
    assert "var canLowPriority = false" in models


def test_both_notification_models_decode_error_kind_with_an_empty_default():
    for path in (PHONE / "Models.swift", PANEL / "Models.swift"):
        models = _read(path)
        assert 'case errorKind = "error_kind"' in models, path
        assert 'errorKind = c.value(.errorKind, "")' in models, path


def test_the_button_is_gated_on_the_flag_and_presses_the_verb():
    view = _read(PHONE / "AgentDetailView.swift")
    assert "if agent.canLowPriority" in view
    assert "lowPriorityBox" in view
    assert "press(.lowPriority, action: PhoneActions.lowPriority" in view
    assert '"session_id": agent.sessionId' in view


def test_the_arm_slot_exists_and_is_cleared_like_the_rest():
    arm = _read(PHONE / "Arm.swift")
    # The enum case once; the `arm(_:)` and `confirm(_:)` switch arms are
    # written dotted, as every other slot's are.
    assert arm.count("case lowPriority") == 1
    assert arm.count("case .lowPriority") == 2
    assert "@Published private(set) var lowPriority: String?" in arm
    # `arm(_:)` and `disarm()` each clear every slot; two `lowPriority = nil`.
    assert arm.count("lowPriority = nil") == 2


def test_the_verb_is_not_a_settling_or_answer_hold_action():
    client = _read(PHONE / "Client.swift")
    assert "PhoneActions.lowPriority" not in client
    for marker in ("    private static let settlingActions",
                   "    private static let answerHoldActions"):
        assert "lowPriority" not in _body(client, marker), marker
