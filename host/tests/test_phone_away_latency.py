# host/tests/test_phone_away_latency.py
"""The away path's *shape*: which leg runs first, and how long each waits.

Source pins over the Swift files, `test_phone_remote.py`'s idiom — `ios/`
has no test target by explicit decision, and this suite is the one that
actually runs.

What is pinned here is the fix for two faults that were one root cause: the
phone walked the home addresses *first*, with the action's own patience on
the first candidate, and only then tried the relay. From a foreign network
those addresses are silent rather than refusing, so every away action paid
8 + 4 × (n−1) seconds before the only leg that could answer was tried — and
PREPARE, whose first-candidate patience is a load-bearing 135s, paid
135 + 30 × (n−1) and read as a hang.

Two rules, and both are ordering rather than shortened timeouts: away, the
relay leg runs first; and a home address that is not the one on file costs
exactly one named probe length.
"""

from __future__ import annotations

import re
from pathlib import Path

from dark_army_daemon import card_prepare, relay_client

ROOT = Path(__file__).resolve().parents[2]

PHONE = ROOT / "ios" / "BobPhone"
CLIENT = PHONE / "Client.swift"
COMPOSER = PHONE / "ComposerView.swift"
BRAND = PHONE / "BrandBar.swift"

# "through the relay" left the sentence on 25 Sep 2026: plain words
# (`plans/2026-09-25-usability-accessibility-pass.md`, V4).
AWAY_LINE = "Writing from away — this can take up to two minutes."


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _body(text: str, signature: str) -> str:
    """The slice from a function's signature to the next `func ` after it.

    Nested local functions are part of the body by design: a relay leg
    written as a local `func viaRelay` is still that method's relay leg.
    """
    start = text.index(signature)
    rest = text[start + len(signature):]
    nxt = rest.find("\n    func ")
    other = rest.find("\n    private func ")
    ends = [i for i in (nxt, other) if i >= 0]
    return rest[:min(ends)] if ends else rest


# --- one probe length, named once ---------------------------------------------


def test_the_probe_length_is_named_once_and_used_everywhere():
    """A spare home address costs one named connect probe, not the action's
    own patience. Five walks share it: post, quietPost, upload, prepareCard
    and the poll's direct leg."""
    text = _read(CLIENT)
    assert text.count("static let directProbe: TimeInterval = 4") == 1
    assert text.count("Self.directProbe") >= 5


def test_no_bare_numeric_secondary_timeout_survives():
    """The old shape — `index == 0 ? 8 : 4` — is what spread five different
    probe lengths through one file. None is left."""
    text = _read(CLIENT)
    assert re.findall(r"index == 0 \? [0-9]+ : [0-9]+", text) == []


def test_every_walk_goes_through_hostCandidates():
    """`post` and `poll` built their candidate lists inline, so a change to
    the order of addresses reached three call sites and missed two. One
    builder, seven callers — including background check-in and catch-up reads."""
    assert _read(CLIENT).count("hostCandidates(record)") == 7


# --- away means the relay leg runs first --------------------------------------

WRITE_PATHS = (
    "func post(",
    "func quietPost(",
    "func prepareCard(",
)


def test_the_write_paths_try_the_relay_before_the_home_walk_when_away():
    text = _read(CLIENT)
    for signature in WRITE_PATHS:
        body = _body(text, signature)
        assert "knowsItIsAway" in body, signature
        assert "hostCandidates" in body, signature
        assert body.index("knowsItIsAway") < body.index("hostCandidates"), (
            f"{signature} still knocks on the home addresses before the relay")


def test_photos_are_refused_before_the_walk_when_away():
    """`/api/upload` is LAN-only by decision, so away there is nothing to
    try. The refusal was already the right sentence — it just arrived after
    60 + 30 × (n−1) seconds of silence."""
    body = _body(_read(CLIENT), "func upload(")
    assert "knowsItIsAway" in body
    assert body.index("knowsItIsAway") < body.index("hostCandidates")
    away = body[body.index("knowsItIsAway"):body.index("hostCandidates")]
    assert "PhoneActions.photosNeedHome" in away


def test_away_is_the_same_field_the_badge_draws():
    """`knowsItIsAway` reads `via`, which is what `BrandBar` draws as AWAY.
    A second derivation is how a screen and a route come to disagree."""
    client = _read(CLIENT)
    # Internal rather than private since the terminal pane's coordinator
    # reads it to choose its feed (`TerminalPane.swift`); still one field.
    assert "var knowsItIsAway: Bool { via == .relay && channel != nil }" \
        in client
    assert client.count("knowsItIsAway: Bool") == 1
    assert "via == .relay" in _read(BRAND)


# --- the prepare deadline is arithmetic ---------------------------------------


def test_the_away_prepare_deadline_covers_the_macs_worst_case():
    """135s was under the sum of the Mac's own worst case: the helper's
    ceiling with attachments plus the connector's idle pickup plus the
    mailbox's ticks. Under it, a prepare the Mac *completed* came back after
    the phone had already called it silent, and the late answer was popped
    and discarded by the next poll."""
    client = _read(CLIENT)
    assert client.count(
        "static let prepareRelayTimeout: TimeInterval = 165") == 1
    assert 165 >= (card_prepare.TIMEOUT_WITH_ATTACHMENTS_SECONDS
                   + relay_client.IDLE_GAP_MAX + 10)
    body = _body(client, "func prepareCard(")
    assert body.count("timeout: Self.prepareRelayTimeout") == 2
    # The direct first candidate keeps its own patience: at home the helper
    # answers on a quiet socket and the timeout is an inactivity timer. The
    # figure now rides the sealed `HomeChannel.request` call.
    assert "timeout: index == 0 ? 135 : Self.directProbe" in body


# --- coming home is automatic -------------------------------------------------


def test_the_poll_asks_the_relay_first_and_still_probes_for_home():
    """The away poll is what turns AWAY off again: the relay answers, then
    one short probe of the address on file — and once a minute, of every
    address, in case the Mac's own address changed. A 200 from that probe
    sets `via = .lan` through the existing assignment."""
    client = _read(CLIENT)
    body = _body(client, "private func pollOnce(_ record: PairingRecord, "
                         "probeHome: Bool = true) async {")
    assert "knowsItIsAway" in body
    assert body.index("pollViaRelay") < body.index("pollDirect"), (
        "the away branch must ask the relay before probing home")
    assert client.count(
        "static let homeRediscoveryInterval: TimeInterval = 60") == 1
    assert "Self.homeRediscoveryInterval" in body
    assert "lastFullDirectWalk = Date()" in body
    assert "Date().timeIntervalSince(lastFullDirectWalk)" in body
    # The probe is a probe: away, even the address on file gets `directProbe`.
    direct = _body(client, "private func pollDirect(")
    assert "(patientFirst && index == 0)" in direct
    assert "Self.directProbe" in direct


def test_a_refresh_a_person_is_waiting_on_skips_the_homecoming_probe():
    """The probe is a 4s wait on an address that is silent by definition,
    and `post` runs a refresh inside its own in-flight window — so paying it
    there put four seconds between every away tap and the button un-dimming.
    It belongs on the timer's polls, which still run every 8s away."""
    client = _read(CLIENT)
    assert "private func pollOnce(_ record: PairingRecord, probeHome: Bool = true) async {" \
        in client
    assert "await poll(record, probeHome: false)" in client
    body = _body(client, "private func pollOnce(_ record: PairingRecord, "
                         "probeHome: Bool = true) async {")
    assert "guard probeHome else { return }" in body
    # The timer's own poll takes the default and keeps probing.
    assert "await self?.poll(record)" in client


def test_one_check_in_at_a_time_and_a_refresh_joins_it():
    """A pull-to-refresh joins the poll already running rather than starting
    a second. Two polls mean two waiters on the same `to-phone` mailbox, and
    a pop is a removal — the loser has already thrown the winner's answer
    away. It also bounds the spinner: a refresh that queued behind a running
    poll's relay leg and then ran its own waited both deadlines end to end."""
    client = _read(CLIENT)
    body = _body(client, "private func poll(_ record: PairingRecord, "
                         "probeHome: Bool = true) async {")
    assert "if let running = polling {" in body
    assert "await running.value" in body
    assert "polling = work" in body
    # And it goes with the timer, cancelled rather than orphaned.
    stop = _body(client, "func stop() {")
    assert "polling?.cancel()" in stop
    assert "polling = nil" in stop


def test_a_cold_launch_goes_the_way_the_last_poll_went():
    """`via` itself is not persisted — the default is still `.lan` — but the
    last successful poll's route is (`lastKnownAwayKey`, the background
    refresh's own note), and `start()` seeds `via = .relay` from it when
    the record carries a relay channel. So an away launch takes `pollOnce`'s
    own away branch on the first poll and lands in seconds rather than
    after a walk of silent home addresses; a phone never away starts `.lan`
    as before. The seed only ever writes `.relay`: a `.lan` guess costs
    nothing, and either guess self-corrects on the first poll."""
    client = _read(CLIENT)
    assert "@Published var via: Via = .lan" in client
    assert "via: PhoneClient.Via = .lan" in client
    start = _body(client, "func start(record: PairingRecord) {")
    assert "seedRouteFromLastPoll()" in start
    seed = _body(client, "private func seedRouteFromLastPoll() {")
    assert "UserDefaults.standard.bool(forKey: Self.lastKnownAwayKey)" in seed
    assert "guard channel != nil" in seed
    assert "via = .relay" in seed
    assert "via = .lan" not in seed


# --- the composer says why the wait is long -----------------------------------


def test_the_composer_says_it_is_writing_from_away():
    """WRITING… from away is a two-minute wait by design. Gated on
    `client.via`, never on `offline`, which is false from away whenever the
    relay is up."""
    text = _read(COMPOSER)
    assert text.count(AWAY_LINE) == 1
    assert "preparing && !offline && client.via == .relay" in text


# --- PREPARE asks again for a lost reply (21 Sep 2026) ----------------------------


def test_prepare_carries_a_command_token_minted_for_it_alone():
    """Every `post` already rides a `command_token` the Mac's receipt
    ledger dedupes on; PREPARE was the one relay write without one, so a
    reply that went missing — popped by a poll that was not waiting for
    it, or posted while the phone slept past the mailbox's expiry — was
    gone for good and WRITING… sat until the deadline. The mark is minted
    for this verb, never through `receipts.open`: a draft is not a pending
    press to list, and `post`'s single `body["command_token"]` site is
    pinned elsewhere."""
    client = _read(CLIENT)
    body = _body(client, "func prepareCard(")
    assert '"command_token": Self.prepareMark()' in body
    assert "receipts.open(" not in body
    mark = _body(client, "static func prepareMark()")
    assert "UUID().uuidString" in mark
    # `command_receipts.TOKEN_RE`: [A-Za-z0-9._-]{8,100}; a prefixed UUID fits.
    import re
    sample = "prepare-" + "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    assert re.fullmatch(r"[A-Za-z0-9._-]{8,100}", sample)


def test_a_lost_prepare_reply_is_asked_for_again_under_the_same_token():
    client = _read(CLIENT)
    body = _body(client, "func prepareCard(")
    # Both relay legs — away-first and the home fallback — recover.
    assert body.count("return await recoverPrepare(answer, body: body, over: channel)") == 2
    recover = _body(client, "private func recoverPrepare(")
    assert "first.status != 202" in recover
    assert "for _ in 0..<Self.prepareReplayAttempts" in recover
    assert "if Task.isCancelled { break }" in recover
    assert "timeout: Self.prepareReplayTimeout" in recover
    assert "if again.status == 202 { stillWriting = parsed; continue }" in recover
    # Attempts exhausted on a Mac still writing say so in the Mac's words,
    # never "the Mac has not answered".
    assert 'if let busy = stillWriting, !busy.detail.isEmpty { return busy }' in recover
    assert "RelayChannel.Trouble.macSilent" in recover
    assert "static let prepareReplayTimeout: TimeInterval = 30" in client
    assert "static let prepareReplayAttempts = 4" in client
    assert "static let prepareReplayGap: TimeInterval = 5" in client
    # The whole recovery window sits inside the Mac's receipt TTL.
    from dark_army_daemon import command_receipts
    assert 165 + 4 * (30 + 5) < command_receipts.RECEIPT_SECONDS

