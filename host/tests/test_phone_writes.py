"""Python source pins over the phone's write path.

`ios/` has no test target, so this is a lint over Swift + `api_server.py` —
`test_phone_url_guard.py`'s pattern. It pins action-name parity with
`LAN_ACTIONS`, the older-Mac 404 copy, that no bearer header travels (the
home path is sealed frames under `X-Bob-Channel`), no SSE, Arm slots, the
plan-gate prefix, and `HostAddress.check` as the home URL builder.
"""

from pathlib import Path

from dark_army_daemon.api_server import ApiServer

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
API_SERVER = ROOT / "host" / "dark_army_daemon" / "api_server.py"

ABSENT = (
    "wrap_up",
    "enroll_project",
    "reveal_session",
)

#: Every `Arm.Slot`. `manualClear` and `review` are the card screen's Mark
#: checked / Mark reviewed — their own slots so Start cannot re-aim one.
#: `merge` and `mergeFix` are the Done card's MERGE and Fix (3 Oct 2026).
ARM_SLOTS = (
    "start",
    "startHere",
    "refine",
    "stop",
    "deleteCard",
    "deleteAgent",
    "close",
    "permission",
    "done",
    "lowPriority",
    "clearDone",
    "manualClear",
    "merge",
    "mergeFix",
    # Passed / Failed on a card flagged with a check file (25 Sep 2026, the
    # manual check folder plan): its own slot, so a Mark checked arm can
    # never confirm an outcome.
    "manualOutcome",
    "review",
    # End on the Comm tab: closes Mission Control's terminal, its own slot.
    "missionEnd",
    # The Prep row's batch Refine (25 Sep 2026): its own slot, so a Refine
    # armed on a card screen can never confirm a batch, nor the reverse.
    "refineBatch",
    # The Backlog row's batch Start (25 Sep 2026): its own slot, never a card screen's Start.
    "startBatch",
)

OLDER_MAC = "this Mac cannot take commands from the phone yet"
PLAN_GATE = "this card has no plan yet"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _phone_swift() -> str:
    files = list(PHONE.glob("*.swift"))
    assert files, "no Swift sources under ios/BobPhone"
    return "\n".join(path.read_text() for path in files)


def test_every_lan_action_is_named_in_actions_swift():
    actions = _read(PHONE / "Actions.swift")
    names = ApiServer.LAN_ACTIONS
    assert names, "LAN_ACTIONS must not be empty"
    for name in names:
        assert f'"{name}"' in actions, f"{name} missing from Actions.swift"


def test_unchosen_verbs_are_not_named_as_phone_actions():
    actions = _read(PHONE / "Actions.swift")
    for name in ABSENT:
        assert f'"{name}"' not in actions, f"{name} enrolled in Actions.swift"


def test_the_older_mac_copy_is_the_specified_sentence_once():
    actions = _read(PHONE / "Actions.swift")
    assert actions.count(OLDER_MAC) == 1


def test_phone_never_sends_a_bearer_header():
    """No device token and no loopback token travel from this app: every
    home request is a sealed frame, and the only header that names the
    phone is `X-Bob-Channel` — derived from the key, not a credential."""
    actions = _read(PHONE / "Actions.swift")
    assert '"X-Bob-Channel"' in actions
    for path in PHONE.glob("*.swift"):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("///"):
                continue
            assert "X-Bob-Device" not in line, (
                f"{path.name} sends the device token: {stripped}")
            assert "X-Bob-Token" not in line, (
                f"{path.name} sends the loopback token: {stripped}")


def test_phone_swift_never_opens_sse():
    for path in PHONE.glob("*.swift"):
        text = path.read_text()
        assert "/api/events" not in text, path.name
        assert "text/event-stream" not in text, path.name


def test_arm_has_distinct_slots_and_a_timeout():
    arm = _read(PHONE / "Arm.swift")
    for slot in ARM_SLOTS:
        assert slot in arm, f"Arm is missing slot {slot}"
    assert "timeoutSeconds" in arm


def test_a_confirm_in_a_list_must_name_the_row_it_confirms():
    """One `Arm` drawn over many rows must not let a slot armed on one row
    confirm the *first* press on another.

    The outbox's waiting cards are the only list in the app that does this,
    and the failure was silent: the second row's button still read `REMOVE`
    — the arming label — while that single tap destroyed a card holding the
    person's own typed words, with no undo. `confirm` now takes the id and
    matches it; a detail screen, which owns one subject and disarms on
    navigation, passes nothing and is unchanged."""
    arm = _read(PHONE / "Arm.swift")
    assert "func confirm(_ slot: Slot, id: String = \"\") -> Bool {" in arm
    assert "guard let armedId = armed, id.isEmpty || armedId == id else {" in arm
    board = _read(PHONE / "BoardView.swift")
    assert "arm.confirm(.deleteCard, id: entry.id)" in board


def test_the_outbox_rows_controls_are_real_tap_targets():
    """11pt ink is a ~13pt-tall target, and these two sit eight points
    apart with one of them destroying a card. `BrandBar`'s 44pt hit area is
    the app's own rule; these are the rows that were missing it."""
    board = _read(PHONE / "BoardView.swift")
    outbox_row = board[board.index("private struct OutboxRow"):]
    assert outbox_row.count(".frame(minWidth: 44, minHeight: 44)") == 2
    assert outbox_row.count(".contentShape(Rectangle())") >= 2
    composer = _read(PHONE / "ComposerView.swift")
    assert composer.count(".frame(minWidth: 44, minHeight: 44)") == 2


def test_plan_gate_prefix_is_the_daemon_s_opening_words():
    phone = _phone_swift()
    assert PLAN_GATE in phone
    daemon = _read(ROOT / "host" / "dark_army_daemon" / "daemon.py")
    assert PLAN_GATE in daemon


def test_action_url_is_built_with_host_address_check():
    actions = _read(PHONE / "Actions.swift")
    assert "HostAddress.check" in actions
    assert '"/api/home"' in actions
    assert '"/api/action"' not in actions


def test_menu_in_progress_never_sends_skip_from_pending():
    """Launcher-off Menu tap is not the confirm. skip_plan_gate rides
    only pressStart after confirm(.start).

    Matched on the argument rather than the whole call: the helper also
    carries which control to relabel (`as:`), and that is not what this
    pin is about."""
    card = _read(PHONE / "CardDetailView.swift")
    assert "updateInProgress(skipPlanGate: pendingSkip" not in card
    assert "updateInProgress(skipPlanGate: false" in card
    assert "updateInProgress(skipPlanGate: true" in card
    arm = _read(PHONE / "Arm.swift")
    assert "onDisarm" in arm
    assert "pendingSkip = false" in card
    assert "pendingPlainMove = false" in card


def test_phone_codex_hide_and_note_decode_tolerantly():
    source = (PHONE / 'Models.swift').read_text()
    for name, fallback in [('canHide', 'false'), ('interactionNote', '""')]:
        assert f'{name} = c.value(.{name}, {fallback})' in source
    assert 'case canHide = "can_hide"' in source
    assert 'case interactionNote = "interaction_note"' in source
