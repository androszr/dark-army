"""Pins for the phone card screen's in-flight feedback and its optimistic delete.

`ios/` has no test target by explicit decision, so the Swift half here is a
Python lint over the source — `test_phone_action_feedback.py`'s pattern, and
this file is its card-screen sibling: that one pins the agent screen, this one
pins the same three seams (`writeInFlight`, a settling hold, one scoped `post`)
on `CardDetailView` plus the two things a card needs that an agent does not —
a card-keyed settling set pruned against the *board*, and a post-write refresh
that is guaranteed to be a read made after the write.

The daemon half pins the ordering the phone's optimism rests on: `delete_card`
returns True only after `_publish_board()`, so the very next board state the
Mac serves cannot list the card. Nothing in the daemon changed for this work —
the pin exists so that ordering cannot be quietly rearranged out from under
the phone.
"""

import re
from pathlib import Path

import pytest

from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
CLIENT = PHONE / "Client.swift"
CARD = PHONE / "CardDetailView.swift"
BOARD_VIEW = PHONE / "BoardView.swift"
MODELS = PHONE / "Models.swift"
ARM = PHONE / "Arm.swift"

NOTICE = "The Mac accepted the delete, but its board still lists this card."


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _body(text: str, header: str) -> str:
    """The source from `header` to the line that closes it at the
    declaration's own indent. `header` carries that indent, so every caller
    below names a member one level inside its type."""
    start = text.find(header)
    assert start >= 0, f"no {header!r}"
    indent = header[:len(header) - len(header.lstrip(" "))]
    assert indent, f"{header!r} must carry its own indent"
    end = text.find(f"\n{indent}}}", start)
    assert end > start, f"{header!r} never closes"
    return text[start:end]


# --- The card settling set ----------------------------------------------------


def test_the_card_settling_set_is_published_and_read_only_outside():
    client = _read(CLIENT)
    assert client.count(
        "@Published private(set) var settlingCards: Set<String> = []") == 1


def test_an_accepted_delete_settles_on_both_legs_of_post():
    """Home and relay are two legs of one `post`, and the relay one is the
    one somebody forgets. Each `beginSettlingCard` sits with the session
    settling it mirrors, so a leg that grew one and not the other shows up
    here rather than as a card that reappears only when away."""
    client = _read(CLIENT)
    calls = [m.start() for m in re.finditer(r"beginSettlingCard\(", client)]
    assert len(calls) == 3, f"beginSettlingCard appears {len(calls)} time(s)"
    anchors = [m.start() for m in re.finditer(r"beginSettling\(", client)]
    paired = [c for c in calls
              if any(0 < c - a < 400 for a in anchors)]
    assert len(paired) == 2, (
        "expected both legs of post to settle the card beside the session; "
        f"found {len(paired)}")


def test_the_delete_verb_is_not_in_the_session_keyed_settling_set():
    """`settlingActions` is keyed by session id and `pruneSettling` clears
    against the agent list. A card id in there would never be cleared —
    the card would stay hidden for the whole backstop, every time."""
    client = _read(CLIENT)
    actions = _body(client, "    private static let settlingActions")
    assert "boardDelete" not in actions


def test_the_card_hold_is_cleared_by_the_board_and_never_by_the_200():
    """The 200 says the daemon took the verb; the board it serves next is
    the proof. Both snapshot writers must prune, and each beside the
    session prune it mirrors."""
    client = _read(CLIENT)
    calls = [m.start() for m in re.finditer(r"pruneSettlingCards\(\)", client)]
    assert len(calls) == 3, f"pruneSettlingCards appears {len(calls)} time(s)"
    anchors = [m.start() for m in re.finditer(r"pruneSettling\(\)", client)]
    paired = [c for c in calls if any(0 < c - a < 120 for a in anchors)]
    assert len(paired) >= 2, (
        f"only {len(paired)} of the card prunes sit beside a session prune")
    body = _body(client, "    private func pruneSettlingCards()")
    assert "snapshot.board.cards" in body, (
        "the card hold must be pruned against the board, not the agents")


def test_the_lapsed_backstop_says_so_in_one_sentence_spelled_once():
    client = _read(CLIENT)
    assert client.count("deleteUnsettledNotice") == 2
    assert client.count(NOTICE) == 1
    elsewhere = [p.name for p in PHONE.glob("*.swift")
                 if p != CLIENT and NOTICE in p.read_text()]
    assert not elsewhere, f"the sentence is spelled again in {elsewhere}"


# --- The post-write refresh ---------------------------------------------------


def test_only_one_check_in_ever_runs_at_a_time():
    """Two polls mean two waiters on one relay mailbox, where a pop is a
    removal and the loser has already thrown the winner's answer away.
    `refreshAfterWrite` must reach the mailbox only through `poll`."""
    client = _read(CLIENT)
    assert client.count("polling = work") == 1
    assert client.count("pollOnce(") == 2, "pollOnce grew a second caller"
    body = _body(client, "    func refreshAfterWrite() async")
    assert "pollOnce(" not in body
    assert "polling" not in body


def test_the_refresh_a_write_is_dimmed_behind_is_a_read_after_the_write():
    client = _read(CLIENT)
    assert client.count("pollsStarted += 1") == 1
    assert client.count("pollsCompleted += 1") == 1
    poll = _body(client, "    private func poll(_ record: PairingRecord")
    assert "pollsStarted += 1" in poll
    assert "pollsCompleted += 1" in poll
    assert client.count("await refreshNow()") == 0, (
        "post still joins whatever poll is running; a poll issued before "
        "the write still lists what the write removed")
    assert client.count("await refreshAfterWrite()") == 3
    body = _body(client, "    func refreshAfterWrite() async")
    assert "extra < 2" in body, "the loop lost its bound"
    assert "await poll(" in body


def test_pull_to_refresh_keeps_the_plain_join():
    """The spinner-for-ever bug was exactly "wait for the running one, then
    run another". Only `post` gets the two-step rule."""
    body = _body(_read(CLIENT), "    func refreshNow() async")
    assert "await poll(record, probeHome: false)" in body
    assert "while" not in body and "for " not in body


def test_the_in_flight_lookup_keeps_its_pinned_signature():
    """The card screen reads the same seam the agent screen does — it is
    generalised in prose, never forked into a card-shaped twin."""
    client = _read(CLIENT)
    assert client.count(
        "func writeInFlight(for sessionId: String) -> String?") == 1


# --- The card screen ----------------------------------------------------------


def test_the_card_screen_posts_through_one_scoped_helper():
    """One seam, two routes (20 Sep 2026): the guarded Save and Prepare stay
    on the synchronous `post` because the screen reads their reply body;
    every other verb is queued through `enqueue`. Both calls sit inside
    `send`, and both pass the card's id as the scope."""
    text = _read(CARD)
    posts = [line for line in text.splitlines()
             if "client.post" in line or "client.enqueue" in line]
    assert len(posts) == 2, f"CardDetailView posts from {len(posts)} places"
    seam = _body(text, "    private func send(_ action: String")
    assert "client.post(" in seam and "client.enqueue(" in seam
    assert seam.count("scope: scope") == 2 and "let scope = card.id" in seam, (
        "without the scope a card write keys on session_id (absent) and "
        "then the bare verb, so it queues behind nothing and wears no mark")


def test_every_acting_control_dims_and_the_pressed_one_relabels():
    text = _read(CARD)
    assert text.count("SENDING…") >= 7, (
        "thirteen acting controls: Start, Done & clear, Refine, Delete, the "
        "column, assistant and model choosers, the two blocker controls "
        "(CLEAR and BLOCK ON…), Approve this plan, SAVE and the two "
        "save-conflict answers (KEEP MINE and USE THE MAC'S). Only twelve "
        "relabel — the per-id CLEAR shares the BLOCK ON… menu's slot.")
    # Twelve dim on the bare flag; SAVE is the thirteenth and dims on
    # `sending || saveHoldReason != nil`, because it has a second reason to
    # be held that the other twelve do not have.
    # 13 at v17: the "start when planned" tick is a thirteenth acting
    # control on this screen and dims with the rest.
    # 14 with START HERE: spawning Dark Army's own terminal for a card with
    # no connected one is a fourteenth acting control and dims with the rest.
    # 13 again once the "start when planned" tick stopped dimming: it is the
    # one control whose new value is drawn the instant it is pressed
    # (`pendingStartWhenPlanned`), so there is nothing for the person to wait
    # behind — and holding every other button on the card grey for a whole
    # poll cycle, to be told something already on screen, is what "the
    # buttons go dead for a while" was. It keeps `settlingHere`: a card on
    # its way out is not a card to tick.
    # 15 with Mark checked and Mark reviewed: the two acknowledgements are
    # armed acting controls under the steps and the close, and dim with the
    # rest.
    # 16 with the one-next-action band's DONE on an ended run
    # (`plans/2026-09-20-simplify-card-details.md`): the plain move the
    # column mover already offered, now one press on the card's lead, and it
    # dims with the rest.
    # 15 on 20 Sep 2026: the two blocker controls (CLEAR and BLOCK ON…)
    # went with the retired waiting-on feature, and the editor's DISCARD —
    # the card's own words back in every box — joined as an acting control
    # that dims with the rest.
    # Since 20 Sep 2026 a press is queued, not awaited: `sending` is the
    # card *leaving* (a confirmed Delete the Mac accepted) and nothing else,
    # so the sites dim only then, and the pressed control wears the
    # queue's mark (`client.queueMark(for: card.id)`) instead of greying.
    # 16 on 21 Sep 2026: PROMOTE on a Done scout joined as an acting
    # control that dims with the rest.
    # 17 on 22 Sep 2026: DONE & CLOSE above the folds on an ended run
    # joined as an acting control that dims with the rest.
    # 19 on 25 Sep 2026: PASSED and FAILED on a card flagged with a check
    # file joined as two armed acting controls that dim with the rest.
    # 21 with card dependencies (`docs/card-dependencies.md`): WAITS ON's ✕
    # and its Add… picker are acting controls and dim with the rest; the
    # picker relabels with the press's mark (the `.dependencies` slot).
    assert text.count(".disabled(sending)") == 21
    assert "private var sending: Bool { settlingHere }" in text
    assert text.count(".disabled(settlingHere)") == 1
    assert "pendingStartWhenPlanned = nil" in text, (
        "an un-landed tick that the Mac refused, or that belongs to another "
        "card, must never keep being drawn")
    assert text.count(".disabled(sending || saveHoldReason != nil)") == 1
    assert text.count("client.cardLeaving(card.id)") == 1, (
        "the card screen reads the board list's own leaving predicate, so "
        "the two surfaces cannot disagree about a card on its way out")
    assert "client.queueMark(for: card.id)" in text
    assert "client.writeInFlight(for: card.id)" not in text, (
        "the transmit lock is `post`'s own; a screen reading it would grey "
        "every control for the whole of a queued press's relay flight")


def test_a_write_disarms_whatever_was_armed():
    """A stale "Really delete?" must not fire against a card the Mac has
    just changed. The disarm is in the one send seam, not at eight sites."""
    body = _body(_read(CARD), "    private func send(_ action: String")
    assert "arm.disarm()" in body


@pytest.mark.parametrize(
    "label", ["startLabel", "doneLabel", "refineLabel", "deleteLabel"])
def test_sending_wins_over_the_armed_wording(label):
    """The armed copy is a question waiting for a second press; the queue's
    mark (`mark`: QUEUED / SENDING… / SENT, since 20 Sep 2026) is a tap
    already made. Testing armed first would hide the one that matters."""
    body = _body(_read(CARD), f"    private var {label}: String")
    swap = body.find("return mark")
    if swap < 0:
        swap = body.find("SENDING…")
    assert swap >= 0, f"{label} lost the label swap"
    armed = body.find("arm.")
    if armed >= 0:
        assert swap < armed, (
            f"{label} tests the armed wording before the mark")


def test_the_delete_label_holds_through_settling():
    """The 200 arrives before the board proves the card is gone; a screen
    that has not popped yet must not offer Delete again in between."""
    body = _body(_read(CARD), "    private var deleteLabel: String")
    assert "settlingHere" in body


# --- The board list -----------------------------------------------------------


def test_a_card_being_deleted_is_drawn_dimmed_rather_than_hidden():
    """The 2 Sep decision reversed: an accepted `board_delete` used to hide
    the card at once and subtract it from the chip. It is now drawn where it
    was, greyed, saying so, and untappable — so neither the page nor the
    count filters anything, and the chip and the list agree by construction
    rather than by two matching subtractions."""
    text = _read(BOARD_VIEW)
    page = _body(text, "    private func rowBody(for id: String)")
    counter = _body(text, "    private func count(for id: String) -> Int")
    assert "settlingCards" not in page, "the page filters a settling card out"
    assert "settlingCards" not in counter, (
        "the chip still subtracts a card the page now draws")
    assert "max(0" not in counter, (
        "nothing is subtracted any more, so nothing needs a floor")
    link = _body(text, "    private func cardLink(_ card: BoardCard)")
    assert "client.cardLeaving(card.id)" in link, (
        "the row does not ask whether its card is on its way out")
    assert ".disabled(" in link, (
        "a card being destroyed can still be opened")
    # `_body` needs an indented declaration; `PhoneBoardCard` is top level,
    # so slice from its header to the next one.
    start = text.find("struct PhoneBoardCard: View")
    assert start > 0, "no PhoneBoardCard"
    card = text[start:text.find("\nstruct ", start + 1)
                if text.find("\nstruct ", start + 1) > 0 else len(text)]
    card = card[:card.find("\nprivate struct ")] \
        if card.find("\nprivate struct ") > 0 else card
    assert "var leaving" in card, "the card has no disabled state"
    assert "PhoneClient.cardLeavingLine" in card, (
        "greying alone is colour-alone; the card must say the word")


def test_the_raw_card_list_stays_raw():
    """`Outbox.swift`'s landed check reads `board.cards`. Filtering inside
    `Board` would make a queued entry's landing invisible while its twin is
    settling — the filter belongs in the view."""
    body = _body(_read(MODELS),
                 "    func cards(in column: String) -> [BoardCard]")
    assert "settling" not in body
    assert body.count("filter") == 1


def test_no_arm_slot_was_added_for_any_of_this():
    from tests.test_phone_writes import ARM_SLOTS

    arm = _read(ARM)
    found = set(re.findall(r"case (\w+)", _body(arm, "    enum Slot")))
    assert found == set(ARM_SLOTS)


# --- The daemon ordering the phone's optimism rests on ------------------------


def _board_daemon(tmp_path):
    daemon = BobDaemon(headless=True)
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    return daemon, store


@pytest.mark.asyncio
async def test_a_deleted_card_is_absent_from_the_very_next_state(tmp_path):
    """`delete_card` awaits `_publish_board()` before returning True, so a
    read issued *after* the 200 cannot list the card. That is the whole
    basis for the phone hiding it at once — and for the hold ending on the
    board rather than on the reply."""
    daemon, store = _board_daemon(tmp_path)
    try:
        card, _ = store.create({"title": "doomed", "project": "bob"})
        keeper, _ = store.create({"title": "keeper", "project": "bob"})
        ok, _ = await daemon.delete_card(card["id"])
        assert ok is True
        ids = [c["id"] for c in daemon._build_board_state()["cards"]]
        assert card["id"] not in ids
        assert keeper["id"] in ids
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_refused_delete_says_why_in_the_macs_own_words(tmp_path):
    """The refusal string is what the phone draws under the buttons, and a
    refusal never enters settling — the card stays where it was."""
    daemon, store = _board_daemon(tmp_path)
    try:
        ok, detail = await daemon.delete_card("no-such-card")
        assert ok is False
        assert detail.strip()
    finally:
        store.close()
