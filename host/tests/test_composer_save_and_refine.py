"""The card composer has a second button: add the card and refine it in one press.

Both composers — the panel's `BoardCardSheet` in composer mode and the phone's
`ComposerView` — draw it beside the plain save, gated on the daemon's
`dispatch_enabled` (absent, not inert, like the board's own Refine), held by
exactly the conditions that hold the plain button, and sharing that button's
save path under one flag. The flag rides `board_create` as the envelope key
`refine`; the banked (offline) path never carries it.

Source pins in `test_composer_specialists_layout.py`'s style: `ios/` has no
test target, and the panel's request body is built inside the private,
network-bound `DaemonClient.post`, so the shape is pinned here, where every
verification pass runs it.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

PHONE = ROOT / "ios" / "BobPhone" / "ComposerView.swift"
PANEL = ROOT / "panel" / "Sources" / "BobPanel" / "BoardCardSheet.swift"
CLIENT = ROOT / "panel" / "Sources" / "BobPanel" / "BoardClient.swift"

PANEL_BUTTON = 'Button("Add & Refine")'
PANEL_GATE = "if isComposer, board.dispatchEnabled {"
PANEL_SAVE = 'Button(isComposer ? "Add to Prep" : "Save")'
PANEL_CLOSE = 'Button("Close") { state.closeEditor() }'

PHONE_BUTTON = '"SAVE & REFINE"'
# The third term: a scout has no plan to refine, so the button is absent
# for a scout draft (`test_scout_cards.py` pins that side).
PHONE_GATE = 'if board.dispatchEnabled && !offline && kind != "scout" {'


def _read(path: Path) -> str:
    if not path.exists():
        pytest.fail(f"{path} is missing")
    return path.read_text(encoding="utf-8")


def _footer(panel: str) -> str:
    start = panel.index("private var footer: some View {")
    end = panel.index("private var saveHeld: Bool {", start)
    return panel[start:end]


def test_panel_composer_draws_add_and_refine_between_close_and_add_to_prep():
    panel = _read(PANEL)
    assert panel.count(PANEL_BUTTON) == 1
    footer = _footer(panel)
    close = footer.index(PANEL_CLOSE)
    gate = footer.index(PANEL_GATE)
    button = footer.index(PANEL_BUTTON)
    save = footer.index(PANEL_SAVE)
    assert close < gate < button < save
    # Held by exactly the conditions that hold Add to Prep: both buttons
    # read `saveHeld`, gate and hand alike.
    assert footer.count(".disabled(saveHeld)") == 2
    assert footer.count(".clickable(!saveHeld)") == 2
    # The panel does not arm: the session it opens is an interview.
    assert "arm." not in footer and "arm(" not in footer


def test_panel_composer_shares_one_save_path_under_one_flag():
    panel = _read(PANEL)
    assert panel.count("saveComposer(refine: true)") == 1
    assert "private func saveComposer(refine: Bool = false)" in panel
    # The trailing argument moved when `start_when_planned` joined the
    # create; the pin is that one call site carries the flag, not that
    # it is the last argument.
    assert "refine: refine," in panel


def test_panel_client_sends_refine_as_envelope():
    client = _read(CLIENT)
    assert "refine: Bool = false" in client
    assert client.count('body["refine"] = "true"') == 1


def test_phone_composer_draws_save_and_refine_gated_on_reach_and_dispatch():
    phone = _read(PHONE)
    assert phone.count(PHONE_BUTTON) == 1
    assert phone.count(PHONE_GATE) == 1
    assert phone.index(PHONE_GATE) < phone.index("Button(saveRefineLabel)")
    assert phone.index('Button(saving && !refinePressed ? "SAVING…" : "SAVE")') \
        < phone.index(PHONE_GATE)
    # Arm-then-tap, the phone's own Refine restated, keyed on the composer's
    # staging id so a confirm cannot match a slot armed for nothing.
    assert "arm.confirm(.refine, id: stagingId)" in phone
    assert "arm.arm(.refine, id: stagingId)" in phone
    assert "@StateObject private var arm = Arm()" in phone
    assert '"Really refine?"' in phone


def test_phone_banked_path_never_carries_the_flag():
    phone = _read(PHONE)
    save = phone.index("private func save(refine: Bool = false) async {")
    body = phone[save:]
    offline = body.index("if offline || !localPhotos.isEmpty {")
    bank = body.index("bank()", offline)
    flag = body.index('fields["refine"] = "true"')
    post = body.index("client.post(action: PhoneActions.boardCreate")
    assert offline < bank < flag < post
    assert body.count('fields["refine"] = "true"') == 1


def test_live_save_sends_create_token():
    phone = _read(PHONE)
    save = phone.index("private func save(refine: Bool = false) async {")
    body = phone[save:]
    token = body.index('fields["create_token"] = stagingId')
    post = body.index("client.post(action: PhoneActions.boardCreate")
    assert token < post


def test_panel_client_sends_create_token_when_non_empty():
    client = _read(CLIENT)
    assert "createToken: String = \"\"" in client
    assert 'body["create_token"] = createToken' in client


def test_panel_composer_passes_staging_id():
    panel = _read(PANEL)
    assert "createToken: id," in panel or "createToken: id)" in panel
