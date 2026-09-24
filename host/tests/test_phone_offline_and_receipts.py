# host/tests/test_phone_offline_and_receipts.py
"""The phone's offline card cache and its command receipts, pinned from here.

Grep pins over ``ios/BobPhone/*.swift``, ``test_phone_away_window.py``'s
shape: the phone target is not built in CI, so these are what keep the
properties true between Xcode runs. The arithmetic itself is executable, in
``ios/BobPhoneTests/OfflineCacheTests.swift``.

Two of these pin a *shared constant* across the wire — the card-changed
refusal's opening words, and the four settling names — because a rewording on
one side alone is silent on both.
"""

from __future__ import annotations

import pathlib

import pytest

from dark_army_daemon import board

IOS = pathlib.Path(__file__).resolve().parents[2] / "ios"
PHONE = IOS / "BobPhone"


def _text(name: str) -> str:
    path = PHONE / name
    assert path.is_file(), f"{name} moved"
    return path.read_text()


def _all_swift() -> dict:
    files = sorted(PHONE.rglob("*.swift"))
    assert files, "the phone's sources moved"
    return {p.name: p.read_text() for p in files}


# --- the two new files --------------------------------------------------------


def test_the_cache_and_the_ledger_exist_and_are_in_the_project():
    assert (PHONE / "CardCache.swift").is_file()
    assert (PHONE / "Receipts.swift").is_file()
    assert (IOS / "BobPhoneTests" / "OfflineCacheTests.swift").is_file()
    project = (IOS / "BobPhone.xcodeproj" / "project.pbxproj").read_text()
    # Four slots each: build file, file reference, group children, Sources —
    # counted as *lines*, which is what the acceptance grep counts too.
    lines = project.splitlines()
    assert len([l for l in lines if "CardCache.swift" in l]) == 4
    assert len([l for l in lines if "Receipts.swift" in l]) == 4
    assert "OfflineCacheTests.swift" in project


# --- the delta read -----------------------------------------------------------


def test_fetch_card_sync_is_defined_once_and_lives_only_in_the_client():
    """Defined once, called only from the cache's own sweep. A second
    definition would be a second reader of the same door."""
    everywhere = _all_swift()
    definitions = [name for name, text in everywhere.items()
                   if "func fetchCardSync(" in text]
    assert definitions == ["Client.swift"]


def test_the_sweep_is_never_driven_from_the_poll_loop():
    """A read a screen nobody is looking at pays for, on a 4s clock, is the
    thing the delta exists to avoid. It rides `onLive`, beside the outbox."""
    client = _text("Client.swift")
    app = _text("BobPhoneApp.swift")
    assert "cardCache.sync(using: client)" in app
    assert "client.onLive" in app
    # And it is not called from the polling functions.
    for marker in ("func pollOnce", "func poll("):
        assert marker in client


def test_the_background_run_guard_is_on_the_delta_read():
    """`BackgroundRefresh`'s 24 s budget must never be spent filling a cache
    nobody is looking at."""
    client = _text("Client.swift")
    start = client.index("func fetchCardSync(")
    body = client[start:start + 900]
    assert "!backgroundRun" in body


def test_a_404_on_the_new_kind_is_not_an_error():
    """An older Mac has never heard of `card_sync`: the cache simply stays as
    it is."""
    client = _text("Client.swift")
    start = client.index("func fetchCardSync(")
    body = client[start:start + 1900]
    assert "answer.status == 200" in body
    assert "forgetPairing()" in body


# --- the refusal both sides recognise -----------------------------------------

CARD_CHANGED_PREFIX = "this card changed on the Mac"


def test_the_card_changed_prefix_matches_the_daemons_own_words():
    """Byte-for-byte against `board.CARD_CHANGED_REFUSAL`'s opening, so the
    sentence may only ever be extended at its end."""
    assert board.CARD_CHANGED_REFUSAL.startswith(CARD_CHANGED_PREFIX)
    actions = _text("Actions.swift")
    assert f'cardChangedPrefix = "{CARD_CHANGED_PREFIX}"' in actions
    assert "isCardChangedRefusal" in actions


def test_the_panel_recognises_the_same_words():
    panel = (IOS.parent / "panel" / "Sources" / "BobPanel"
             / "ActionModels.swift").read_text()
    assert f'detail.hasPrefix("{CARD_CHANGED_PREFIX}")' in panel


def test_the_card_changed_prefix_is_not_a_prefix_of_the_other_two():
    """Two matchers firing on one refusal would raise the wrong
    confirmation — `isPlanChangedRefusal`'s own argument."""
    actions = _text("Actions.swift")
    for other in ("this card has no plan yet", "this card's plan has changed"):
        assert other in actions
        assert not other.startswith(CARD_CHANGED_PREFIX)
        assert not CARD_CHANGED_PREFIX.startswith(other)


# --- the card screen ----------------------------------------------------------


def test_the_cut_short_lines_are_gone():
    """Replaced by the real text — the live read, or the phone's own copy."""
    detail = _text("CardDetailView.swift")
    assert "cut short — the Mac has the rest" not in detail
    assert detail.count("cut short") == 0


def test_the_card_screen_reads_the_cache_and_saves_guarded():
    detail = _text("CardDetailView.swift")
    assert "client.cardCache.card(seed.id)" in detail
    assert "client.cardCache.planText(id)" in detail
    assert '"expected_revision"' in detail
    assert "isCardChangedRefusal" in detail
    # Both ways out of a conflict, and no third silent path.
    assert "KEEP MINE" in detail
    assert "USE THE MAC'S" in detail


def test_the_plan_is_drawn_with_the_shared_renderer():
    """`Markdown.swift` is byte-pinned to the panel's, so making a plan look
    better on a phone is not a thing that screen may do on its own."""
    detail = _text("CardDetailView.swift")
    # The plan is split at the template's `## Technical detail` heading
    # (`PlanSplit`, pinned by `test_phone_plan_fold.py`); both halves still
    # go through the shared renderer, never a phone-only one.
    assert "PlanSplit.split(plan.text)" in detail
    assert "MarkdownText(source: split.summary" in detail
    assert "MarkdownText(source: detail" in detail


# --- the receipts -------------------------------------------------------------


def test_the_command_token_is_minted_in_exactly_one_place():
    everywhere = _all_swift()
    minting = [name for name, text in everywhere.items()
               if 'body["command_token"]' in text]
    assert minting == ["Client.swift"]
    assert _text("Client.swift").count('body["command_token"]') == 1


def test_the_three_honest_states_and_the_fourth_exist():
    """Plus the queue's two (20 Sep 2026): `queued` — written down at the
    tap, never yet attempted — and `sending`, a transmit in flight, which a
    relaunch reads back as `sent`."""
    receipts = _text("Receipts.swift")
    for state in ("case sent", "case accepted", "case done", "case stuck",
                  "case queued", "case sending"):
        assert state in receipts
    assert receipts.count("case queued") == 1
    assert receipts.count("case sending") == 1
    assert "if out.state == .sending { out.state = .sent }" in receipts


def test_a_stuck_receipt_stays_listed():
    """The whole point of the state: a press whose effect never turns up must
    not fade out as though it worked. And the mirror — a refusal the person
    already read on the screen they pressed from is finished business, not a
    pending press, or `PENDING (n)` fills with plan-gate refusals and the one
    row that matters is lost."""
    receipts = _text("Receipts.swift")
    assert "var outstanding: [Receipt]" in receipts
    assert "!Receipt.finishedStates.contains($0.state)" in receipts
    assert "static let finishedStates: Set<State> = [.done, .refused]" in receipts
    # `.stuck` is never in that set, so the machine never drops it.
    start = receipts.index("private func trim()")
    body = receipts[start:start + 700]
    assert "finishedStates" in body
    assert ".stuck" not in body
    assert 'PENDING (' in _text("ProfileView.swift")
    assert "RETRY" in _text("ProfileView.swift")
    assert "DISCARD" in _text("ProfileView.swift")


def test_only_a_sent_receipt_is_ever_resent():
    """An `accepted` receipt is never re-sent — the Mac already answered, and
    what it waits on is the effect."""
    receipts = _text("Receipts.swift")
    start = receipts.index("func resendable(")
    body = receipts[start:start + 400]
    assert "$0.state == .sent" in body


def test_the_four_settling_names_still_exist_so_no_view_drifted():
    client = _text("Client.swift")
    for name in ("settling", "settlingAnswers", "settlingCards",
                 "settlingPreferences", "boardNotices", "pipelineNotices"):
        assert f"var {name}" in client


def test_both_stores_are_dropped_on_an_unpair():
    """The pairing-scoped stores go with the pairing: a cache filled
    from another Mac must never be read as this one's, a press made against
    it must never be replayed here, and its picture, its buzz log and its
    conversations must never be drawn as this Mac's — and the buzzes banked
    in memory but not yet filed go with the log, or the next unlock files
    the old Mac's under the new pairing's token. The list is now four
    plus the conversation cache."""
    client = _text("Client.swift")
    start = client.index("private func forgetPairing()")
    body = client[start:start + 1300]
    assert "cardCache.forget()" in body
    assert "conversationCache.forget()" in body
    assert "receipts.forget()" in body
    assert "heldPicture.forget()" in body
    assert "notificationLog.forget()" in body
    assert "PushRegistrar.shared.forgetBanked()" in body
    assert "func forgetBanked()" in _text("Push.swift")
    assert "rebase(to:" in _text("Receipts.swift") \
        or "func rebase(to" in _text("Receipts.swift")


def test_the_unlock_gate_drops_the_bank_when_no_pairing_is_loaded():
    """`bank` keeps accepting while the phone is unpaired (an old Mac pushes
    until its own un-pair; the 403 paths unregister nothing), and with no
    record the foreground drain refuses, so a buzz banked after the un-pair
    waited for the next pairing's first drain and was filed under the new
    token. The unlock gate is the one moment the phone is provably
    unpaired: its `else` (no Keychain record after `pairing.load()`) drops
    the bank. Not a guard inside `bank`, which would lose the cold launch's
    tray read, made before `pairing.load()`."""
    app = _text("BobPhoneApp.swift")
    gate = app.index("pairing.load()")
    block = app[gate:gate + 4000]
    branch = block.index("if let record = pairing.record {")
    else_at = block.index("} else {", branch)
    else_body = block[else_at:else_at + 900]
    assert "PushRegistrar.shared.forgetBanked()" in else_body
    # The drain and the drop are the two arms of one `if`: the bank is
    # either filed under the loaded pairing or dropped, never left waiting.
    assert "client.absorbBankedNotifications(token: record.token)" in block[branch:else_at]
    push = _text("Push.swift")
    bank = push[push.index("func bank("):push.index("func takeBanked()")]
    assert "guard record" not in bank and "refusingUntilPaired" not in bank


def test_a_pairing_becoming_current_drops_the_bank_before_its_first_drain():
    """The gate's `else` is one moment; a pairing that lands mid-session is
    another. Between an un-pair and the next scan no gate re-runs, the
    foreground drain refuses (no record) and `bank` keeps accepting an old
    Mac's buzz or a tray read, so the pairing `onChange` → `start(record:)`
    → the first drain under the new record filed them under its token.
    `start` drops the bank on a token change, compared before `stop()` nils
    the record; a same-token restart keeps it. Safe for the cold launch and
    the gate's `.restart` because the unlock block drains the bank before it
    calls `start` — pinned here too, by order."""
    client = _text("Client.swift")
    start = client[client.index("func start(record: PairingRecord)"):]
    start = start[:start.index("\n    func ", 10)]
    drop = start.index("if self.record?.token != record.token {")
    assert "PushRegistrar.shared.forgetBanked()" in start[drop:drop + 200]
    assert drop < start.index("\n        stop()\n")
    assert "forgetBanked" not in start[:drop], "the drop is under the token comparison alone"
    app = _text("BobPhoneApp.swift")
    gate = app.index("pairing.load()")
    block = app[gate:gate + 4000]
    assert block.index("client.absorbBankedNotifications(token: record.token)") \
        < block.index("case .restart: client.start(record: record)")
    # The pairing `onChange` is the second caller — the mid-session path.
    change = app[app.index(".onChange(of: pairing.record)"):]
    assert "client.start(record: record)" in change[:1500]


def test_the_log_is_read_once_per_process():
    """`load()` runs at every unlock while `write()` is a detached task
    chained behind the one before; a re-read with a write pending would
    take the older file, lose the entries just drained from the bank and
    let the gate's next absorb write the older record over the pending
    one. So the store reads once: the guard on `loaded` sits ahead of the
    read, and `forget()` removes the file rather than resetting the flag
    — there is no newer file for a re-pair to read."""
    log = _text("NotificationLog.swift")
    load = log[log.index("func load()"):log.index("func entry(for")]
    assert load.index("guard !loaded else { return }") < load.index("loaded = true")
    assert load.index("loaded = true") < load.index("Data(contentsOf: url)")
    forget = log[log.index("func forget()"):log.index("func settle()")]
    assert "loaded = false" not in forget
    assert "removeItem(at: url)" in forget


# --- what this change may not touch -------------------------------------------


def test_the_three_byte_pinned_files_are_untouched_by_this_change():
    """`Theme.swift`, `Markdown.swift` and `Specialists.swift` are byte-pinned
    to the panel's copies (`test_phone_theme_drift.py`). Nothing here names
    the cache, the receipts or the new read."""
    for name in ("Theme.swift", "Markdown.swift", "Specialists.swift"):
        text = _text(name)
        for word in ("CardCache", "ReceiptLedger", "card_sync",
                     "command_token"):
            assert word not in text, f"{name} names {word}"


# --- the bug audit's fixes, pinned (2026-09-06) -------------------------------


def test_the_save_guard_names_the_revision_of_the_text_on_screen():
    """**The silent overwrite this feature exists to prevent.** The editors
    are seeded from the phone's own copy before `fetchCard` returns — away,
    over the relay, that is tens of seconds — so guarding with the *frame's*
    revision would send `expected_revision: 4` for words typed on top of
    revision 3, the guard would match, and the Mac's revision-4 words would
    be overwritten in silence."""
    detail = _text("CardDetailView.swift")
    start = detail.index("private var shownRevision: Int")
    body = detail[start:start + 900]
    assert "cached?.revision ?? card.revision" in body


def test_save_is_held_whenever_the_text_on_screen_is_not_the_current_copy():
    """The truncation flags alone never fired for a prompt under
    `BOARD_SNAPSHOT_PROMPT_CHARS`, which is most cards."""
    detail = _text("CardDetailView.swift")
    start = detail.index("private var saveHoldReason: String?")
    body = detail[start:start + 1400]
    assert "!haveFullText && cached != nil" in body
    assert "Waiting for the newer version" in body


def test_the_plan_is_read_once_into_state_and_never_from_a_view_body():
    """`draftPrompt` is `@State`, so a `Data(contentsOf:)` in the `plan`
    computed property was one 64 KiB read per keystroke — while editing a
    card out of reach, which is the situation this feature is for."""
    detail = _text("CardDetailView.swift")
    assert "@StateObject private var retained: PhoneCardDraftState" in detail
    assert "get { retained.cachedPlanText }" in detail
    assert "@Published var cachedPlanText: String?" in _text("PhoneSheetHost.swift")
    # The only reads are on the task, never inside `plan`.
    start = detail.index("private var plan: CardPlan? {")
    body = detail[start:start + 800]
    assert "planText(" not in body
    assert "cachedPlanText" in body


def test_every_final_answer_closes_the_receipt():
    """A receipt left in `sent` was re-sent on every `onLive` for ever: an
    unprompted Face ID sheet per relay attempt, a PENDING line contradicting
    the refusal already on screen, and the press finally executing hours
    later when the lease was renewed at home."""
    client = _text("Client.swift")
    start = client.index("func post(action: String")
    body = client[start:client.index("func quietPost(")]
    # The 409 is no longer the only close.
    assert "receipts.refuse(mark, detail: refusal, surfaced: firstPress)" in body
    assert "receipts.refuse(mark, detail: PhoneActions.olderMac," in body
    assert body.count("receipts.refuse(") >= 3
    # Transport loss keeps `holdOff`, which is what leaves it resendable.
    assert body.count("receipts.holdOff(mark)") >= 3
    # A press the person declined at the Face ID sheet never left the phone.
    assert "receipts.remove(mark)" in body


def test_a_resend_is_bounded_and_never_replays_what_can_be_checked():
    receipts = _text("Receipts.swift")
    client = _text("Client.swift")
    assert "static let maxAttempts" in receipts
    assert "$0.attempts < Self.maxAttempts" in receipts
    assert "func exhausted(" in receipts
    start = client.index("func flushReceipts()")
    body = client[start:start + 3600]
    assert "ReceiptLedger.evidenceBeforeSending(receipt.effect," in body
    # The attempt is counted where the sender takes the record (`.queued`
    # or `.sent` → `.sending`), so the ceiling and the record agree.
    assert "receipts.markSending(receipt.id)" in body
    assert "ReceiptLedger.replayWindow" in body
    # The window give-up wears its own sentence, not the ceiling's count.
    assert "ReceiptLedger.tooOldLine" in body


def test_the_cache_writes_off_the_main_actor_and_only_when_changed():
    """`remember(board:)` runs on every board frame; without both guards that
    is a multi-megabyte encode and a protected atomic write every four
    seconds, on the thread drawing the board."""
    cache = _text("CardCache.swift")
    start = cache.index("private func persist(force: Bool = false)")
    assert "Task.detached(priority: .utility)" in cache[start:start + 900]
    assert "lastChangeKey" in cache
    # The directory walk runs where the held set can actually have changed.
    start = cache.index("func remember(board: Board)")
    body = cache[start:start + 1500]
    assert "if dropped { evictPlansPastBudget() }" in body


def test_the_cache_is_pairing_scoped_by_construction():
    """`Receipt.pairingToken`'s twin. The explicit un-pair must drop as much
    as an involuntary 403 does, and a re-pair against another Mac must never
    read the old Mac's cards."""
    cache = _text("CardCache.swift")
    client = _text("Client.swift")
    app = _text("BobPhoneApp.swift")
    assert "var pairingToken: String = \"\"" in cache
    assert "func adopt(_ token: String)" in cache
    # Rebased where a pairing becomes the current one, before any sweep.
    start = client.index("func start(record: PairingRecord)")
    body = client[start:start + 2200]
    assert "cardCache.adopt(record.token)" in body
    assert "receipts.rebase(to: record.token)" in body
    # And the deliberate un-pair reaches the same teardown as the 403.
    assert "func forgetPairedState()" in client
    assert "client.forgetPairedState()" in app


def test_pending_names_the_press_in_words_and_its_subject():
    receipts = _text("Receipts.swift")
    profile = _text("ProfileView.swift")
    assert "var wording: String" in receipts
    assert "var subject: String" in receipts
    assert "receipt.wording" in profile
    assert "receipt.subject" in profile
    # The raw wire verb is no longer the row title.
    assert "Text(receipt.action)" not in profile


def test_done_scope_changed_is_the_clear_done_effect():
    receipts = _text("Receipts.swift")
    tests = (IOS / "BobPhoneTests" / "OfflineCacheTests.swift").read_text()
    assert "doneScopeChanged" in receipts
    assert "doneScopeChanged" in tests


# --- the phone target actually parses (2026-09-06 delta audit) ----------------


def test_every_phone_source_parses():
    """**The floor, and the only compile-adjacent phone check that always
    runs.**

    `.github/workflows/tests.yml` now has a `phone` job running `xcodebuild
    test`, but it is **path-gated**: a push that touches nothing under
    `ios/**` does not compile the phone at all. This test does, on every
    push, inside the always-on `host` job — and on a developer's machine
    before pushing, in about four seconds rather than the several minutes a
    simulator build takes. It also skips cleanly with no toolchain, which
    `xcodebuild` cannot.

    It is here because an unterminated string literal in `Receipts.swift`
    once sat behind a green pytest, a clean ruff and a clean `swift build`
    (which covers `panel/` alone) and shipped a target that would not build.

    `swiftc -parse` is **syntax only**: no type checking, no linking, no
    simulator. A type error still needs the phone job or Xcode; do not
    overclaim this. The widget under `ios/BobPhoneWidget` is an appex the
    same rule applies to, so its sources are parsed here too — and so are
    `ios/Shared`, the one folder **both** targets compile, and both test
    bundles.
    """
    import shutil
    import subprocess

    if shutil.which("xcrun") is None:
        pytest.skip("no Xcode toolchain on this machine")
    try:
        probe = subprocess.run(["xcrun", "--find", "swiftc"],
                               capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("no Xcode toolchain on this machine")
    if probe.returncode != 0:
        pytest.skip("no Xcode toolchain on this machine")

    sources = sorted(PHONE.glob("*.swift")) \
        + sorted((IOS / "BobPhoneTests").glob("*.swift")) \
        + sorted((IOS / "BobPhoneWidgetTests").glob("*.swift")) \
        + sorted((IOS / "BobPhoneWidget").glob("*.swift")) \
        + sorted((IOS / "BobPhoneNotification").glob("*.swift")) \
        + sorted((IOS / "Shared").glob("*.swift"))
    assert sources, "the phone's sources moved"

    def parse(paths):
        try:
            return subprocess.run(["xcrun", "swiftc", "-parse", *map(str, paths)],
                                  capture_output=True, timeout=300)
        except subprocess.SubprocessError:
            pytest.skip("the toolchain would not run")

    # One invocation for every source — `-parse` is per file anyway, and one
    # driver launch is a few seconds where a launch per file was 35 s. Only a
    # red run pays for the per-file pass, to name the file that broke.
    if parse(sources).returncode == 0:
        return
    broken = []
    for path in sources:
        done = parse([path])
        if done.returncode != 0:
            broken.append(
                f"{path.name}: "
                + done.stderr.decode("utf-8", "replace").strip().splitlines()[0])
    assert broken != [], "swiftc rejected the sources together but every file alone"
    assert broken == [], "\n".join(broken)


def test_a_card_revision_is_never_evidence_before_sending():
    """`.cardRevision` carries a payload the predicate does not describe: a
    save that transport-failed still holds the person's typed words, and any
    other write on that card moves the number past `atLeast`. Dropping the
    receipt there throws those words away with no line in PENDING; re-sending
    gets a 409 that keeps them and shows what moved. `.doneScopeChanged` is
    the same shape and sits on the same `return false` arm: any other Done
    arrival/departure moves the token, and dropping the receipt would forget
    a sweep that never ran. `.replyHold` (20 Sep 2026) is the third: a
    queued reply carries the person's words, and the row leaving `waiting`
    before the sender took it is no reason to drop them unsent."""
    receipts = _text("Receipts.swift")
    start = receipts.index("static func evidenceBeforeSending(")
    body = receipts[start:start + 400]
    assert "case .cardRevision, .doneScopeChanged, .replyHold, .none:" in body
    assert "return false" in body
    # And `settled(against:)` keeps the full test — there the Mac answered
    # 200, so the press demonstrably landed.
    start = receipts.index("func settled(against snapshot: Snapshot)")
    assert "Self.landed(" in receipts[start:start + 900]


def test_a_refusal_the_person_read_is_not_filed_as_pending():
    """`surfaced` is decided in the one place that knows: a first press had a
    screen behind it, a resend did not."""
    client = _text("Client.swift")
    assert "let firstPress = token.isEmpty" in client
    assert client.count("surfaced: firstPress") == 3
    receipts = _text("Receipts.swift")
    assert "func refuse(_ id: String, detail: String, surfaced: Bool = false)" \
        in receipts
    assert "$0.state = surfaced ? .refused : .stuck" in receipts


def test_the_save_guard_follows_the_text_on_both_branches():
    """The iteration-1 blocker's twin, on the fetched branch: the drafts are
    seeded from the fetched row once, so a card edited on the Mac after that
    read landed moves the *frame* while the editors still show the fetched
    text. The panel's `applyFetchedCard` already moves the number with the
    text; this does now too."""
    detail = _text("CardDetailView.swift")
    start = detail.index("private var shownRevision: Int")
    body = detail[start:start + 900]
    assert "if let row = fetched { return row.revision }" in body
    assert "return card.revision }" not in body


def test_the_cache_encode_leaves_the_main_actor_too():
    """Moving the write off and leaving the encode on paid back only half:
    at `MAX_CARDS` with prompts to `MAX_PROMPT_CHARS` a full `JSONEncoder`
    run every four seconds is most of the original cost."""
    cache = _text("CardCache.swift")
    start = cache.index("private func persist(force: Bool = false)")
    body = cache[start:start + 900]
    assert "Task.detached(priority: .utility)" in body
    assert "JSONEncoder().encode(rows)" in body
    # The guard is a hash, not an encode, and it ignores `lastSeen`.
    assert "private var changeKey: Int" in cache
    assert "lastChangeKey" in cache
    assert "hasher.combine(row.planDigest)" in cache
    key_start = cache.index("private var changeKey: Int")
    assert "lastSeen" not in cache[key_start:key_start + 500]


def test_exhausted_uses_its_pairing_parameter():
    """A parameter that looks like a guard and is not one is how the next
    caller gets it wrong."""
    receipts = _text("Receipts.swift")
    start = receipts.index("func exhausted(pairingToken: String)")
    body = receipts[start:start + 400]
    assert "$0.pairingToken == pairingToken" in body


def test_an_empty_board_still_prunes_the_cache():
    """`available` is *stated* by the daemon, so `available: true` with no
    cards is a genuinely empty board. A second `!cards.isEmpty` guard left
    rows on disk that nothing would ever prune or read."""
    cache = _text("CardCache.swift")
    start = cache.index("func remember(board: Board)")
    body = cache[start:start + 200]
    assert "guard board.available else { return }" in body


def test_a_404_latches_so_an_older_mac_is_not_asked_for_ever():
    """A Mac that predates the kind fills nothing, so every later pass would
    find the whole board stale and ask again — one sealed frame per live
    poll, against the one Mac that can do nothing with it."""
    client = _text("Client.swift")
    cache = _text("CardCache.swift")
    start = client.index("func fetchCardSync(")
    body = client[start:start + 1800]
    assert "answer.status == 404 { cardSyncUnsupported = true }" in body
    # Cleared where a pairing becomes the current one, so an upgraded Mac is
    # asked again rather than latched off for the life of the install.
    assert "func start(record: PairingRecord)" in client
    after_start = client[client.index("func start(record: PairingRecord)"):]
    assert "cardSyncUnsupported = false" in after_start[:1200]
    # And the sweep is what reads it, above the request it would otherwise
    # send, with the upgrade escape hatch beside it.
    sweep = cache[cache.index("func sync(using client:"):]
    sweep = sweep[:sweep.index("func planStamps") if "func planStamps" in sweep
                  else 1200]
    assert "client.cardSyncUnsupported" in sweep
    assert "$0.revision > 0" in sweep
    assert sweep.index("client.cardSyncUnsupported") < sweep.index(
        "await client.fetchCardSync")
