"""Source pins over the phone's attachments, Prepare and keyboard behaviour.

`ios/` has no test target, so this is a lint over the Swift sources in
`test_phone_writes.py`'s house style. It pins the photo picker to one file,
the upload route's single spelling, the Prepare verb and its slow timeout,
the `prepare_enabled` decode, and — the part with no other net at all —
which fields autocorrect and which stay verbatim.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"

COMPOSER = PHONE / "ComposerView.swift"
ANSWER_BOX = PHONE / "AnswerBox.swift"
PAIRING = PHONE / "Pairing.swift"
CLIENT = PHONE / "Client.swift"
ACTIONS = PHONE / "Actions.swift"
MODELS = PHONE / "Models.swift"
OUTBOX = PHONE / "Outbox.swift"

#: The two prose surfaces this plan handed back to the ordinary keyboard.
PROSE_FILES = (COMPOSER, ANSWER_BOX)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    text = path.read_text()
    assert text.strip(), f"empty file: {path}"
    return text


def _swift_files() -> list[Path]:
    files = sorted(PHONE.glob("*.swift"))
    assert files, "no Swift sources under ios/BobPhone"
    return files


# --- photos -------------------------------------------------------------------


def test_the_photo_picker_lives_in_the_composer_and_nowhere_else():
    named = [p.name for p in _swift_files() if "PhotosPicker" in p.read_text()]
    assert named == [COMPOSER.name], named


def test_the_composer_imports_photosui_and_mints_a_lowercased_staging_id():
    text = _read(COMPOSER)
    assert "import PhotosUI" in text
    assert "UUID().uuidString.lowercased()" in text


def test_the_composer_sends_the_returned_path_not_the_name_it_asked_for():
    """The Mac dedupes a name collision, so only its answer is the truth."""
    text = _read(COMPOSER)
    assert "staged.append(result.detail)" in text
    assert 'fields["attachments"] = staged.joined(separator: "\\n")' in text


# --- the upload route ---------------------------------------------------------


def test_the_upload_path_has_exactly_one_spelling_in_the_app():
    named = [p.name for p in _swift_files() if '"/api/upload"' in p.read_text()]
    assert named == [ACTIONS.name], named
    # The URL is built in one place, the sealed home transport, through the
    # accessor that reads that one spelling.
    assert "PhoneActions.uploadURL" in _read(CLIENT.parent / "HomeTransport.swift")
    assert 'static let uploadPath = "/api/upload"' in _read(ACTIONS)


def test_the_upload_request_is_sealed_and_carries_no_bearer_header():
    text = _read(CLIENT)
    assert "func upload(stagingId:" in text
    assert "home.upload(" in text
    assert "X-Bob-Device" not in text
    assert "X-Bob-Token" not in text


# --- prepare ------------------------------------------------------------------


def test_prepare_card_is_named_and_carries_the_slow_timeout():
    actions = _read(ACTIONS)
    client = _read(CLIENT)
    assert '"prepare_card"' in actions
    assert "func prepareCard(" in client
    # 135s: the desktop panel's own prepare timeout, over the helper's 120s
    # ceiling with attachments. `post`'s 8s would orphan every one of them.
    # The spare addresses behind it are one short connect probe each —
    # a silent host must not spend the whole of that patience.
    assert "timeout: index == 0 ? 135 : Self.directProbe" in client
    # Away, the relay carries it, and its deadline has to cover the
    # mailbox's own pickup on top of the helper's ceiling.
    assert "timeout: Self.prepareRelayTimeout" in client


def test_prepare_reads_the_body_that_post_throws_away():
    actions = _read(ACTIONS)
    assert "struct PhonePrepareResult" in actions
    for key in ('"prompt"', '"workflow"'):
        assert key in actions, key


def test_prepare_enabled_is_decoded_and_gates_the_button():
    models = _read(MODELS)
    assert models.count("prepare_enabled") == 1
    assert "prepareEnabled = c.value(.prepareEnabled, false)" in models
    # Absent, never inert: the button is inside the flag's own `if`.
    assert "if board.prepareEnabled {" in _read(COMPOSER)


# --- the keyboard -------------------------------------------------------------


def test_prose_fields_no_longer_switch_autocorrect_off():
    for path in PROSE_FILES:
        text = _read(path)
        assert "autocorrectionDisabled" not in text, path.name
        assert text.count("textInputAutocapitalization(.sentences)") == 1, path.name


def test_the_pairing_identifiers_stay_exactly_as_typed():
    text = _read(PAIRING)
    assert text.count("autocorrectionDisabled") == 2
    assert "textInputAutocapitalization(.never)" in text
    assert "textInputAutocapitalization(.characters)" in text
    assert "textInputAutocapitalization(.sentences)" not in text


def test_the_form_lowers_the_keyboard_on_a_scroll_drag():
    """The platform's own behaviour, on the composer's one `ScrollView`."""
    text = _read(COMPOSER)
    assert text.count(".scrollDismissesKeyboard(.immediately)") == 1


def test_the_background_tap_lowers_the_keyboard():
    """A tap in the gaps clears the focus — and the well keeps its own tap,
    so this pin cannot be satisfied by deleting the thing it protects."""
    text = _read(COMPOSER)
    assert text.count("focusedField = nil") == 1
    assert ".onTapGesture { focusedField = nil }" in text
    # Anti-vacuous: the field well's own tap and its call site are still here.
    assert "onTap: { focusedField = key }" in text
    assert ".fieldWell(" in text


def test_the_dismiss_gesture_does_not_compete_with_the_controls():
    """The tap is a background layer, never a gesture over the form: anything
    on the `ScrollView` itself swallows the first press on the mic, the three
    menus, PREPARE, the photo picker and both save buttons."""
    text = _read(COMPOSER)
    assert "simultaneousGesture" not in text
    assert "highPriorityGesture" not in text
    assert text.count(".onTapGesture") == 1


def test_only_the_composer_changed():
    """The scope answer, asserted rather than trusted. The board's rows
    joined the composer on 21 Sep 2026 (`BoardView.swift`, the search's
    way out — `test_phone_board_search.py`); nothing else dismisses a
    keyboard by scrolling. On 25 Sep 2026 every scrolling screen that holds
    a text box joined them (the card, the agent sheet's conversation and
    details, the comm log — `test_phone_keyboard_hide.py`); the tap-away
    stays the composer's alone. The scout reports list, with its search
    box, joined on 25 Sep 2026, and the plans list, its twin, the same
    day."""
    drags = {"BoardView.swift", "CardDetailView.swift", "AgentDetailView.swift",
             "ConversationView.swift", "CommView.swift", "ScoutReportsView.swift",
             "PlansView.swift"}
    for path in _swift_files():
        if path == COMPOSER:
            continue
        text = _read(path)
        if path.name not in drags:
            assert "scrollDismissesKeyboard" not in text, path.name
        assert "focusedField = nil" not in text, path.name


# --- the upload gate ----------------------------------------------------------
#
# A card saved while a photo is still climbing the wire is written with an
# empty `attachments` field: the copy lands a second later, is referenced by
# nothing, and ages out of the Mac's orphan sweep. The photo was never on the
# card — so both verbs are held while `uploading`, at the button and inside
# the function, and the hold says so on screen.


def test_save_is_held_while_a_photo_is_still_uploading():
    text = _read(COMPOSER)
    assert "guard !tool.isEmpty, !saving, !uploading, !staging else { return }" in text
    assert ".disabled(tool.isEmpty || saving || preparing || uploading || staging)" in text


def test_prepare_is_held_while_a_photo_is_still_uploading():
    """Prepare sends the staged list too, so it waits on the same batch."""
    text = _read(COMPOSER)
    # `prepareRouteAvailable` is "the Mac, or this phone's own key": the
    # photo hold is unchanged on both routes.
    assert (
        "guard canPrepare, !preparing, !saving, !uploading, !staging,\n"
        "              prepareRouteAvailable else { return }"
    ) in text
    assert (
        ".disabled(!canPrepare || preparing || saving || uploading"
        " || staging || !prepareRouteAvailable)"
    ) in text


def test_the_hold_names_itself_rather_than_only_dimming():
    assert '"photos are still sending…"' in _read(COMPOSER)


def test_an_upload_refusal_without_a_sentence_still_says_something():
    assert '"that photo did not reach the Mac"' in _read(COMPOSER)


# --- the one-box idea ---------------------------------------------------------


def test_the_composer_draws_the_idea_box_and_it_types_like_prose():
    """The box the whole feature is: a multiline prose field, first in the
    form, with the mic beside it like every other typed field."""
    text = _read(COMPOSER)
    assert '@State private var idea = ""' in text
    assert 'field("your idea", text: $idea, axis: .vertical,' in text
    assert 'dictation: "composer.idea"' in text
    # Prose, so the keyboard helps: the file asserts autocapitalisation once
    # for every field it draws, and `field` is the one that draws this.
    assert ".textInputAutocapitalization(.sentences)" in text


def test_the_composer_shows_specialists_as_faces_not_a_box():
    """The specialists Prepare names are drawn as faces alone: the typed box
    that listed the same names above them is gone, and so is its microphone."""
    text = _read(COMPOSER)
    assert 'field("expected specialists"' not in text
    assert '"composer.specialists"' not in text


def test_prepare_can_run_from_either_box():
    text = _read(COMPOSER)
    assert "private var prepareInputEmpty: Bool {" in text
    assert "!prepareInputEmpty && !tool.isEmpty && !projectRoot.isEmpty" in text


def test_the_prepare_body_carries_the_idea():
    """One key on the same body, so the relay leg and every direct candidate
    carry it without a second route."""
    text = _read(CLIENT)
    assert '"idea": idea,' in text
    assert "idea: String = \"\"" in text


def test_the_press_sends_the_idea_under_both_names_for_an_older_mac():
    text = _read(COMPOSER)
    assert "summary: typedSummary.isEmpty ? thought : summary," in text
    assert "idea: thought)" in text


def test_a_returned_field_is_applied_only_when_it_is_not_empty():
    """An older Mac answers without title/summary, which reads as empty —
    and empty must never blank what somebody typed."""
    text = _read(COMPOSER)
    assert "if !result.title.isEmpty { title = result.title }" in text
    assert "if !result.summary.isEmpty { summary = result.summary }" in text


def test_the_prepare_result_decodes_the_two_new_fields_tolerantly():
    text = _read(ACTIONS)
    assert 'title: (obj?["title"] as? String) ?? ""' in text
    assert 'summary: (obj?["summary"] as? String) ?? ""' in text


def test_the_prepare_result_decodes_the_suggested_folder_tolerantly():
    text = _read(ACTIONS)
    assert 'suggestedRoot: (obj?["suggested_root"] as? String) ?? ""' in text
    assert 'var suggestedRoot: String = ""' in text


def test_the_composer_holds_the_suggestion_as_view_state_only():
    """It is on no card, no store column, no snapshot and in no banked draft,
    so a resumed draft never carries a stale opinion."""
    text = _read(COMPOSER)
    assert "@State private var suggestedRoot" in text
    assert "@State private var revertRoot" in text
    # The draft's own field list, which is what gets banked.
    assert "suggestedRoot" not in _read(OUTBOX)
    assert "revertRoot" not in _read(OUTBOX)
    assert ("[title, summary, prompt, tool, model, projectRoot, workflow, "
            "idea," in text)
    line = [ln for ln in text.splitlines()
            if "[title, summary, prompt, tool, model, projectRoot" in ln][0]
    assert "suggestedRoot" not in line
    assert "revertRoot" not in line


def test_the_suggestion_applies_itself_and_offers_one_way_back():
    """The Mac's pick moves the picker on arrival; the line under it is one
    button naming the previous project, in the panel's exact words."""
    text = _read(COMPOSER)
    # Applied inside prepare(): the picker and the slot move together.
    assert "projectRoot = pick.root" in text
    assert "suggestedRoot = pick.root" in text
    # A second, independent guard: only a project this phone can select.
    assert "private var revertProject: BoardProject? {" in text
    # One way back, worded as the panel words it, and drawn exactly once.
    assert text.count('Button("Use \\(') == 1
    assert text.count(' instead") {') == 1
    assert 'Text("Dark Army picked \\(' in text
    assert '"keep mine"' not in text
    assert '"use it"' not in text
    # Cleared at the top of the press, on the picker's buttons, and by the
    # revert button itself.
    assert text.count('revertRoot = ""') >= 3


def test_the_draft_carries_the_idea_and_counts_it_as_typed_work():
    text = _read(OUTBOX)
    assert 'var idea: String = ""' in text
    assert "case workflow, idea, staged, localPhotos, open, updatedAt" in text
    assert 'idea = c.value(.idea, "")' in text
    assert "[title, summary, prompt, workflow, idea].contains {" in text


# --- the prepare button sits under the idea box ---


def _prepare_block(text: str) -> str:
    """The `prepareBlock` declaration alone, up to whatever is declared next."""
    start = text.index("private var prepareBlock")
    rest = text.index("private ", start + len("private var prepareBlock"))
    return text[start:rest]


def test_prepare_is_drawn_once_and_between_the_idea_and_title_fields():
    """The flow reads the way it works: one box, one press, the rest fills in."""
    text = _read(COMPOSER)
    assert text.count('"PREPARE"') == 1
    assert text.count("if board.prepareEnabled {") == 1
    body_start = text.index("var body: some View")
    idea = text.index('field("your idea"', body_start)
    call = text.index("prepareBlock", idea)
    title = text.index('field("title"', body_start)
    assert body_start < idea < call < title, (body_start, idea, call, title)


def test_the_prepare_notes_travel_with_the_button():
    text = _read(COMPOSER)
    block = _prepare_block(text)
    for line in ("Writing from away — this can take up to two minutes.",
                 "Prepare needs the Mac, which is out of reach right now."):
        assert line in block, line
        assert text.count(line) == 1, line


def test_save_stays_at_the_bottom_with_the_save_facts():
    text = _read(COMPOSER)
    assert text.count('"SAVE"') == 1
    body_start = text.index("var body: some View")
    save = text.index('"SAVE"')
    picker = text.index("projectPicker\n", body_start)
    title = text.index('field("title"', body_start)
    assert picker < save, (picker, save)
    saved = ("This card will be saved on your phone and sent to the board "
             "when the Mac is back in reach.")
    assert text.count(saved) == 1
    assert text.index(saved) > save
    for hold in ("photos are still sending…",
                 "photos are being saved on this phone…"):
        assert title < text.index(hold) < save, hold


def test_the_error_note_is_one_slot():
    """`note` is written by the photo paths, by Prepare and by Save alike, so
    a second one under the button would either double every photo refusal or
    invent a rule about which writer last wrote it."""
    text = _read(COMPOSER)
    assert text.count("Text(note)") == 1
    body_start = text.index("var body: some View")
    picker = text.index("projectPicker\n", body_start)
    assert picker < text.index("Text(note)") < text.index('"SAVE"')


# --- the objective, typed before the card exists ---


def test_the_objective_boxes_are_drawn_only_against_a_mac_that_takes_them():
    """Absent, never present and refused: an older Mac 403s the whole sealed
    create when any objective key rides along."""
    models = _read(MODELS)
    assert models.count("objective_on_create_supported") == 1
    assert ("objectiveOnCreateSupported = "
            "c.value(.objectiveOnCreateSupported, false)") in models
    text = _read(COMPOSER)
    assert text.count("if board.objectiveOnCreateSupported {") == 1
    gate = text.index("if board.objectiveOnCreateSupported {")
    block = text[gate:text.index('"SAVE"', gate)]
    for label in ('field("who benefits"', 'field("intended benefit"',
                  'field("success criterion"'):
        assert block.count(label) == 1, label
    assert ".lineLimit(" not in block


def test_prepare_fills_an_objective_box_only_when_it_is_empty_on_the_phone():
    text = _read(COMPOSER)
    for key in ("beneficiary", "intendedBenefit", "successCriterion"):
        assert (f"if !result.{key}.isEmpty && {key}.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {{\n"
                f"                {key} = result.{key}\n") in text, key


def test_save_adds_each_objective_key_only_when_typed():
    text = _read(COMPOSER)
    start = text.index("private func save(refine: Bool = false)")
    body = text[start:]
    for key, name in (("beneficiary", "beneficiary"),
                      ("intended_benefit", "intendedBenefit"),
                      ("success_criterion", "successCriterion")):
        assert (f"if !{name}.isEmpty {{\n"
                f'            fields["{key}"] = {name}\n') in body, key
    outbox = _read(OUTBOX)
    for key, name in (("beneficiary", "beneficiary"),
                      ("intended_benefit", "intendedBenefit"),
                      ("success_criterion", "successCriterion")):
        assert (f"if !entry.{name}.isEmpty {{\n"
                f'            fields["{key}"] = entry.{name}\n') in outbox, key


def test_the_draft_and_the_outbox_carry_the_objective():
    outbox = _read(OUTBOX)
    assert outbox.count("case beneficiary, intendedBenefit, successCriterion") == 2
    assert outbox.count('beneficiary = c.value(.beneficiary, "")') == 2
    text = _read(COMPOSER)
    assert '_beneficiary = State(initialValue: d?.beneficiary ?? "")' in text
    assert "beneficiary, intendedBenefit, successCriterion, expanded ? \"1\" : \"\", \"\\u{0}\"]" in text


def test_the_prepare_result_decodes_the_objective_tolerantly():
    actions = _read(ACTIONS)
    assert 'beneficiary: (obj?["beneficiary"] as? String) ?? ""' in actions
    assert 'intendedBenefit: (obj?["intended_benefit"] as? String) ?? ""' in actions
    assert 'successCriterion: (obj?["success_criterion"] as? String) ?? ""' in actions
