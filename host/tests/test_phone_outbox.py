"""Source pins over the phone's offline card queue, plus the daemon
contracts its sync leans on.

`ios/` has no test target the gates run, so the Swift half is a lint over the
sources in `test_phone_writes.py`'s house style. The Python half is the part
that would actually make state *wrong* rather than absent: the join keys the
duplicate check compares, and the summary the snapshot may clamp under the
phone's feet.
"""

from pathlib import Path

import pytest

from dark_army_daemon import attachments
from dark_army_daemon.board import BoardStore
from dark_army_daemon import board as board_module
from dark_army_daemon.daemon import BobDaemon

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"

OUTBOX = PHONE / "Outbox.swift"
COMPOSER = PHONE / "ComposerView.swift"
BOARD_VIEW = PHONE / "BoardView.swift"
CLIENT = PHONE / "Client.swift"
APP = PHONE / "BobPhoneApp.swift"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"


def _swift_function(text: str, signature: str) -> str:
    """One Swift function's source, signature included, by brace matching.

    A plain substring search over the whole file would let a pin pass on a
    line that happens to sit in a neighbouring function — which is exactly
    the mistake these gates exist to catch.
    """
    start = text.index(signature)
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


#: The Save half of the old joined sentence — it is the fact that still
#: applies when the Mac's card-writing helper is switched off and there is no
#: Prepare button on the screen at all.
EXPLAINER = (
    "This card will be saved on your phone and sent "
    "to the board when the Mac is back in reach."
)

#: The Prepare half, which travelled up under the button it is about.
PREPARE_NEEDS_MAC = "Prepare needs the Mac, which is out of reach right now."

#: The joined form the two were split out of. It must be gone.
OLD_JOINED = "Prepare needs the Mac. This card"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    text = path.read_text()
    assert text.strip(), f"empty file: {path}"
    return text


def _swift_files() -> list[Path]:
    files = sorted(PHONE.glob("*.swift"))
    assert files, "no Swift sources under ios/BobPhone"
    return files


# --- the store exists and is registered ---------------------------------------


def test_the_outbox_is_a_real_file_in_the_phone_target():
    _read(OUTBOX)
    project = _read(PBXPROJ)
    # A file reference and a build-phase entry; a source Xcode knows about but
    # never compiles is the failure this catches.
    assert project.count("Outbox.swift") >= 2


def test_the_queue_is_written_behind_the_phone_s_own_lock():
    """Card text is the person's own words; it gets the same protection the
    Face ID gate gives the fleet."""
    text = _read(OUTBOX)
    assert ".completeFileProtection" in text
    # Read only after the gate opens, beside the Keychain read.
    assert "outbox.load()" in _read(APP)


# --- the sweep ----------------------------------------------------------------


def test_only_a_scarred_entry_doubts_itself():
    """A fresh card sends unconditionally, so two deliberately identical
    cards both land; only a send that may already have arrived looks first."""
    text = _read(OUTBOX)
    assert "entry.attempts > 0" in text


def test_the_scar_is_written_before_the_post_not_after():
    """A send cut off mid-air must leave the mark that arms the check."""
    text = _read(OUTBOX)
    scar = 'note(current.id) { $0.attempts += 1; $0.lastError = "" }'
    assert scar in text
    post = "client.post(action: PhoneActions.boardCreate"
    assert post in text
    assert text.index(scar) < text.index(post)


def test_an_uncertain_entry_waits_while_the_board_cannot_be_checked():
    text = _read(OUTBOX)
    assert "if uncertain(entry), !client.snapshot.board.available {" in text


def test_the_queue_goes_in_the_order_it_was_written():
    text = _read(OUTBOX)
    assert "sorted(by: { $0.createdAt < $1.createdAt })" in text


def test_a_transport_failure_is_never_painted_on_an_entry_as_a_refusal():
    """`post`'s in-flight collision answers with an empty detail. Recording
    that would paint a blank error on a perfectly healthy card."""
    text = _read(OUTBOX)
    assert "static let transportSentences: Set<String>" in text
    for sentence in ('""', '"Could not reach the Mac."', '"Bad address."',
                     '"no longer paired"'):
        assert sentence in text, sentence
    assert "Self.transportSentences.contains(result.detail)" in text


def test_photos_go_before_the_card():
    text = _read(OUTBOX)
    assert "sendPhotos(of: entry.id, using: client)" in text
    assert text.index("sendPhotos(of: entry.id") < text.index(
        "client.post(action: PhoneActions.boardCreate")


def test_the_big_file_work_stays_off_the_main_actor():
    """A 20 MB write on the main actor stalls the poll loop and every
    button on screen."""
    text = _read(OUTBOX)
    assert text.count("Task.detached(priority: .utility)") >= 2


# --- the composer -------------------------------------------------------------


def test_the_explainer_is_spelled_once_and_only_in_the_composer():
    for line in (EXPLAINER, PREPARE_NEEDS_MAC):
        named = [p.name for p in _swift_files() if line in p.read_text()]
        assert named == [COMPOSER.name], (line, named)
        assert _read(COMPOSER).count(line) == 1, line


def test_the_joined_offline_sentence_is_gone():
    """The Save fact stays at the bottom beside Save; the Prepare fact went
    up under Prepare. Joined, neither one could sit beside its own button."""
    named = [p.name for p in _swift_files() if OLD_JOINED in p.read_text()]
    assert named == [], named


def test_the_photo_picker_is_still_the_composer_s_alone():
    named = [p.name for p in _swift_files() if "PhotosPicker" in p.read_text()]
    assert named == [COMPOSER.name], named


def test_the_offline_save_banks_the_card_instead_of_posting_it():
    text = _read(COMPOSER)
    assert "if offline || !localPhotos.isEmpty {\n            bank()\n            return\n        }" in text
    assert "outbox.enqueue(" in text


def test_the_mac_vanishing_between_poll_and_press_banks_too():
    """That is the offline case one frame late, and gets the same answer."""
    text = _read(COMPOSER)
    assert 'result.detail == "Could not reach the Mac."' in text


def test_the_pickers_fall_back_to_the_remembered_lists():
    text = _read(COMPOSER)
    assert "return live.available ? live : outbox.catalogueBoard()" in text


def test_the_photo_ceiling_counts_what_is_still_on_the_phone():
    text = _read(COMPOSER)
    assert "max(0, 8 - staged.count - localPhotos.count)" in text


def test_the_catalogue_is_not_rewritten_on_every_four_second_poll():
    text = _read(OUTBOX)
    assert "guard data != lastCatalogueBytes else { return }" in text


def test_a_worded_refusal_backs_off_instead_of_hammering_every_poll():
    """An entry the Mac refused *in words* used to be re-POSTed on every 4s
    poll — and each 409 dragged a whole extra state refresh behind it — for
    as long as the refusal stood. A refusal now arms a doubling backoff
    (10s up to 5 min) that the sweep respects; transport failures
    deliberately do not back off, because they already stop the sweep."""
    text = _read(OUTBOX)
    assert "var heldUntil: Double = 0" in text
    assert "var retryDelay: Double = 0" in text
    assert "backoffStart: TimeInterval = 10" in text
    assert "backoffCap: TimeInterval = 300" in text
    hold = _swift_function(text, "private func hold(")
    assert "entry.retryDelay * 2" in hold
    assert "min(Self.backoffCap," in hold
    # Both refusal sites arm it: the card leg and the photo leg.
    assert text.count("hold(current.id, error: result.detail)") == 1
    assert text.count("hold(id, error: result.detail)") == 1
    # The sweep skips a held entry — after the landed check, so a card that
    # did arrive is still removed while held.
    sync = _swift_function(text, "func sync(using client: PhoneClient)")
    landed_at = sync.index("alreadyLanded(entry")
    held_at = sync.index("?.held == true")
    send_at = sync.index("sendPhotos(of: entry.id")
    assert landed_at < held_at < send_at
    # Persisted like every other field, tolerantly.
    assert "heldUntil = c.value(.heldUntil, 0)" in text
    assert "retryDelay = c.value(.retryDelay, 0)" in text


def test_the_backoff_has_a_visible_retry_beside_remove():
    """The hold must never read as a stuck queue: a person can override it,
    and their press resets the doubling — it is a fresh decision, not
    attempt n+1."""
    outbox = _read(OUTBOX)
    retry = _swift_function(outbox, "func retryNow(")
    assert "entry.heldUntil = 0" in retry
    assert "entry.retryDelay = 0" in retry
    board = _read(BOARD_VIEW)
    # The label swaps to SENDING… while the sweep is actually running, so the
    # button is matched on its idle word rather than on the whole call.
    assert '"RETRY"' in board
    assert "outbox.retryNow(entry.id)" in board
    # Beside REMOVE, on the entry, and only where a refusal is worn.
    row = _swift_function(board, "private struct OutboxRow")
    assert '"RETRY"' in row
    assert '"REMOVE"' in row
    assert "!entry.lastError.isEmpty" in row


# --- the visible queue --------------------------------------------------------


def test_the_queue_is_shown_on_prep_and_on_the_no_board_screen():
    text = _read(BOARD_VIEW)
    assert text.count("waitingSection") >= 3
    assert 'WAITING FOR THE MAC' in text


def test_removing_a_queued_card_takes_two_presses_on_that_card():
    """Two presses, and both of them on the *same row*. This is the app's
    only list drawn over one `Arm`, so the confirm has to name the row it
    confirms — see `test_phone_writes.py`'s own pin — or a card armed here
    lets a first press anywhere else in the queue delete un-armed."""
    text = _read(BOARD_VIEW)
    assert "arm.confirm(.deleteCard, id: entry.id)" in text
    assert "arm.arm(.deleteCard, id: entry.id)" in text
    assert "arm.disarm()" in text


def test_a_refusal_is_shown_in_the_mac_s_own_words():
    text = _read(BOARD_VIEW)
    assert "entry.lastError.isEmpty" in text
    assert "OutboxStore.waitingLine" in text


# --- the trigger --------------------------------------------------------------


def test_the_sweep_rides_the_existing_poll_and_nothing_else():
    client = _read(CLIENT)
    assert "var onLive: (() -> Void)?" in client
    assert "onLive?()" in client
    app = _read(APP)
    assert "client.onLive = {" in app
    assert "outbox.remember(board: client.snapshot.board)" in app
    assert "Task { await outbox.sync(using: client) }" in app


# --- the daemon contracts the join leans on -----------------------------------


def test_the_daemon_strips_the_two_join_keys_so_the_phone_stores_them_trimmed(
        tmp_path):
    """`BoardStore.create` strips title and summary. The phone trims at
    enqueue for exactly this reason — otherwise its own copy of the card
    would never match the one the Mac stored."""
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    try:
        card, detail = store.create({
            "title": "  a card  ", "summary": "  a sentence  ",
            "tool": "claude", "column_name": "prep",
        })
    finally:
        store.close()
    assert card is not None, detail
    assert card["title"] == "a card"
    assert card["summary"] == "a sentence"
    assert "trimmingCharacters(in: .whitespacesAndNewlines)" in _read(OUTBOX)


def test_a_long_summary_can_be_clamped_in_the_snapshot_so_the_join_allows_it():
    """The store keeps up to `MAX_SUMMARY_CHARS`, the snapshot publishes at
    most `BOARD_SNAPSHOT_SUMMARY_CHARS` and *says so* with
    `summary_truncated`. The phone must match on the prefix in that case, or
    a long card would resend for ever."""
    assert (board_module.MAX_SUMMARY_CHARS
            > BobDaemon.BOARD_SNAPSHOT_SUMMARY_CHARS)
    text = _read(OUTBOX)
    assert "if card.summaryTruncated {" in text
    assert "summary.hasPrefix(card.summary)" in text


def test_the_duplicate_check_looks_only_at_prep():
    """Every agent-written and phone-written card lands in Prep, and the
    board snapshot lists every non-done card, so the check can be exact."""
    assert 'card.column == "prep"' in _read(OUTBOX)


def test_fields_for_sends_create_token():
    fields = _swift_function(_read(OUTBOX), "private func fields(for")
    assert 'fields["create_token"] = entry.id' in fields
    assert "entry.id.isEmpty" in fields


def test_already_landed_prefers_create_token_any_column():
    """Token match is first, any column, no scar. The Prep title heuristic
    still exists and still requires `attempts > 0`."""
    landed = _swift_function(_read(OUTBOX), "private func alreadyLanded(")
    token_at = landed.index("createToken")
    prep_at = landed.index('card.column == "prep"')
    assert token_at < prep_at
    assert "uncertain(entry)" in landed
    assert "entry.attempts > 0" in _swift_function(
        _read(OUTBOX), "private func uncertain(")


def test_a_retried_photo_can_never_clobber_the_one_that_landed(tmp_path,
                                                               monkeypatch):
    """The upload leg may re-send a photo the Mac already stored. The store
    suffixes rather than overwrites — which is what makes the retry safe."""
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", tmp_path / "att")
    folder = "abcd1234-efgh5678-ijkl9012-mnop34"
    first, detail = attachments.store_upload(folder, "photo-1.png", b"one")
    assert detail is None
    second, detail = attachments.store_upload(folder, "photo-1.png", b"one")
    assert detail is None
    assert first != second
    for rel in (first, second):
        assert (tmp_path / "att" / rel).is_file()


def test_discard_does_not_delete_a_folder_another_composer_holds():
    """`TabView` keeps an inactive tab's hierarchy alive and the banked draft is
    one slot for the whole phone, so two mounted composers can share a staging
    id. Discard on the second one deleted the photo folder the first was still
    filling — it went on showing thumbnails whose files were gone, and queuing
    it would have named missing paths. The delete is now gated on nothing but
    the discarding composer holding the id."""
    outbox = OUTBOX.read_text()
    assert "func holdComposer" in outbox
    assert "func releaseComposer" in outbox
    assert "func composerHolderCount" in outbox
    clear = _swift_function(outbox, "func clearDraft")
    assert "composerHolderCount(id) <= 1" in clear, (
        "clearDraft deletes the staging folder without asking who else is "
        "holding it")
    assert "deletingPhotos" in clear


def test_the_composer_holds_and_releases_its_staging_id():
    """The register is only true if the pair is actually wired to the view's
    lifetime. `onAppear`/`onDisappear` pair up across a push and a pop, which
    is why the count is a count rather than a flag."""
    composer = COMPOSER.read_text()
    assert "outbox.holdComposer(stagingId)" in composer
    assert "outbox.releaseComposer(stagingId)" in composer
    hold = composer.index("outbox.holdComposer(stagingId)")
    assert ".onAppear" in composer[max(0, hold - 200):hold], (
        "the hold must be taken when the form appears")


def test_the_orphan_sweep_still_spares_the_banked_draft():
    """The gate above defers the delete to the sweep, so the sweep's own
    exemption for the banked draft is now load-bearing twice over."""
    outbox = OUTBOX.read_text()
    sweep = _swift_function(outbox, "func sweepOrphanFolders")
    assert "draft?.id" in sweep, (
        "the sweep no longer spares the banked draft's folder")

