# host/tests/test_phone_reconnect_display.py
"""The phone's offline state: still trying, and when it will try next.

Source pins over the Swift files, `test_phone_away_latency.py`'s idiom —
`ios/` has no test target by explicit decision, and this suite is the one
that actually runs.

Three things are pinned, and each of them is a thing the next reader will be
tempted to change: the retry cadence (4s at home, 8s through the mailbox) is
observed by the display and never driven by it; the countdown is *published*
by the poll loop rather than invented in the drawing code; and the failure
sentence the Mac and the relay send is shown verbatim and never reworded.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PHONE = ROOT / "ios" / "BobPhone"
CLIENT = PHONE / "Client.swift"
BRAND = PHONE / "BrandBar.swift"
APP = PHONE / "BobPhoneApp.swift"
BACKGROUND = PHONE / "BackgroundRefresh.swift"
RELAY = PHONE / "RelayTransport.swift"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _body(text: str, signature: str) -> str:
    """The slice from a function's signature to the next `func ` after it."""
    start = text.index(signature)
    rest = text[start + len(signature):]
    ends = [i for i in (rest.find("\n    func "),
                        rest.find("\n    private func ")) if i >= 0]
    return rest[:min(ends)] if ends else rest


def _struct(text: str, name: str) -> str:
    """A top-level `struct <name>` declaration, up to the next top-level one."""
    start = text.index(f"struct {name}")
    rest = text[start:]
    ends = [i for i in (rest.find("\nstruct ", 1),
                        rest.find("\nfinal class ", 1),
                        rest.find("\nextension ", 1)) if i > 0]
    return rest[:min(ends)] if ends else rest


NOTE_SIG = "private func noteAttempt() -> UInt64 {"
START_SIG = "func start(record: PairingRecord) {"
STOP_SIG = "func stop() {"


# --- the cadence is observed, never driven ------------------------------------


def test_the_poll_cadence_is_unchanged():
    text = _read(CLIENT)
    assert text.count("8_000_000_000") == 1
    # The pause itself, plus `start()`'s weak-self fallback.
    assert text.count("4_000_000_000") == 2
    body = _body(text, NOTE_SIG)
    assert "8_000_000_000" in body
    assert re.search(r"\(via == \.relay\)\s*\n?\s*\?\s*8_000_000_000\s*:\s*"
                     r"4_000_000_000", body), body


def test_the_loop_sleeps_what_noteAttempt_returned():
    body = _body(_read(CLIENT), START_SIG)
    assert body.count("Task.sleep(nanoseconds:") == 1
    assert "Task.sleep(nanoseconds: pause)" in body
    assert re.search(r"let pause = await self\?\.noteAttempt\(\)", body), body


def test_the_loop_no_longer_reads_via_itself():
    body = _body(_read(CLIENT), START_SIG)
    # Code only: the slice runs up to the next `func`, so it trails the
    # doc comment of whatever declaration comes next.
    code = "\n".join(line for line in body.splitlines()
                     if not line.strip().startswith("//"))
    assert "via" not in code, code


def test_noteAttempt_is_called_from_one_site():
    text = _read(CLIENT)
    # The declaration and the single call.
    assert text.count("noteAttempt()") == 2
    background = _read(BACKGROUND)
    assert "noteAttempt" not in background


def test_a_landed_poll_resets_and_only_unreachable_counts():
    body = _body(_read(CLIENT), NOTE_SIG)
    assert "case .live: link.failures = 0" in body
    assert "case .unreachable: link.failures += 1" in body
    unpaired = re.search(r"case \.unpaired[^\n]*", body)
    assert unpaired, body
    assert "link.failures" not in unpaired.group(0)


def test_the_note_is_published_and_written_only_here():
    text = _read(CLIENT)
    assert "@Published private(set) var link = LinkAttempt()" in text
    # Every field write lives in `noteAttempt()`; `stop()` resets the whole
    # struct. Nothing else assigns into it.
    assert text.count("link.nextAttemptAt =") == 1
    assert text.count("link.waitSeconds =") == 1
    assert text.count("link.route =") == 1


def test_stop_clears_the_note():
    body = _body(_read(CLIENT), STOP_SIG)
    assert "link = LinkAttempt()" in body


# --- the bar invents nothing --------------------------------------------------


def test_the_bar_invents_nothing():
    bar = _struct(_read(BRAND), "ReconnectBar")
    assert "@State" not in bar
    assert "@ObservedObject" not in bar
    assert "+= 1" not in bar
    assert "Timer" not in bar


def test_the_failure_sentence_is_never_reworded():
    brand = _read(BRAND)
    assert "let sentence: String" in brand
    assert brand.count("let sentence: String") == 1
    relay = _read(RELAY)
    trouble = relay[relay.index("enum Trouble {"):]
    trouble = trouble[:trouble.index("\n    init(")]
    literals = re.findall(r'"([^"]{12,})"', trouble)
    assert literals, "no Trouble sentences found to pin"
    for literal in literals:
        assert literal not in brand, literal


def test_the_away_badge_is_still_the_only_via_read_in_the_view_layer():
    brand = _read(BRAND)
    # One comparison — `PhoneBrandBar`'s own AWAY badge — plus the away
    # line's single hand-off of `via` into `AwaySpan`, which owns the
    # window's wording for both this screen and the profile. `ReconnectBar`
    # is handed `link.route` and reads no `via`.
    assert brand.count("away.via == .relay") == 1
    assert brand.count("via: away.via") == 1
    assert brand.count("away.via") == 2
    assert "client.via" not in brand
    assert "AwayState.shared.via" not in brand
    bar = _struct(brand, "ReconnectBar")
    assert "away.via" not in bar
    app = _read(APP)
    assert "client.via" not in app
    assert "AwayState.shared.via" not in app


def test_the_state_says_its_name_in_words():
    brand = _read(BRAND)
    for phrase in ("RECONNECTING", "attempt ", "retry in ", "retrying now"):
        assert phrase in brand, phrase


def test_the_progress_is_determinate_and_silent_to_voiceover():
    bar = _struct(_read(BRAND), "ReconnectBar")
    assert "█" in bar and "░" in bar
    cells = bar[bar.index("Text(cells(remaining: left))"):]
    assert ".accessibilityHidden(true)" in cells[:400], cells[:400]


def test_the_first_connect_screen_shows_it_only_after_a_failure():
    view = _struct(_read(BRAND), "ConnectingView")
    # The genuine first wait is Dark Army's typed line, never a platform spinner.
    assert "ProgressView" not in view
    assert "AgentChatterView(.line, wait: .connecting" in view
    assert "ReconnectBar(" in view
    assert "link.failures > 0" in view


def test_the_status_bar_renders_the_bar_rather_than_a_bare_sentence():
    app = _read(APP)
    assert "ReconnectBar(" in app
    # The fallback wording is unchanged, at both call sites.
    assert app.count('"Could not reach the Mac."') == 2


# --- the check-in says which rung it is on ------------------------------------

POLL_SIG = ("private func poll(_ record: PairingRecord, "
            "probeHome: Bool = true) async {")
POLL_ONCE_SIG = ("private func pollOnce(_ record: PairingRecord, "
                 "probeHome: Bool = true) async {")
POLL_DIRECT_SIG = "private func pollDirect("
POLL_RELAY_SIG = "private func pollViaRelay("
SEED_SIG = "private func seedRouteFromLastPoll() {"


def _phase_strings(client: str) -> list[str]:
    """Every literal in `PhoneClient.Phase`'s table — the words the screens
    must never hold a copy of."""
    table = client[client.index("enum Phase {"):]
    table = table[:table.index("\n    }\n") + 1]
    literals = re.findall(r'"([^"\\\n]{12,})"', table)
    # `homeOther` interpolates a count, so its literal holds a `\(`; pin its
    # leading words as well, or it would be the one string not checked.
    literals += re.findall(r'"([^"\n]{12,}?)\s*\\\(', table)
    assert literals, "no Phase strings found to pin"
    assert any("another home address" in s for s in literals), literals
    return literals


def test_the_phase_is_written_in_one_place():
    text = _read(CLIENT)
    assert '@Published private(set) var phase = ""' in text
    # The declaration, `notePhase`'s body and `stop()`'s reset. Every rung
    # goes through `notePhase`; nothing else assigns the field.
    assert text.count("phase = ") == 3
    assert 'private func notePhase(_ text: String) { phase = text }' in text
    for path in (BRAND, APP, BACKGROUND, RELAY):
        assert "notePhase(" not in _read(path), path.name


def test_the_phase_is_stamped_at_every_rung():
    text = _read(CLIENT)
    direct = _body(text, POLL_DIRECT_SIG)
    assert "Phase.homeOnFile" in direct
    assert "Phase.homeOther(" in direct
    # The away probe behind a relay answer is worded by `pollOnce`, so the
    # walk itself stamps only on the home rule.
    assert "if patientFirst {" in direct
    relay = _body(text, POLL_RELAY_SIG)
    assert "notePhase(Phase.relayAsking)" in relay
    assert relay.index("Phase.relayAsking") < relay.index("channel.request(")
    once = _body(text, POLL_ONCE_SIG)
    assert "notePhase(Phase.homeProbe)" in once
    assert once.index("Phase.homeProbe") < once.index("pollDirect(")
    start = _body(text, START_SIG)
    assert "channel?.onSent = {" in start
    sent = start[start.index("channel?.onSent = {"):]
    sent = sent[:sent.index("\n        }") + 1]
    assert "notePhase(Phase.relayWaiting)" in sent
    poll = _body(text, POLL_SIG)
    assert 'notePhase("")' in poll
    assert poll.index("await work.value") < poll.index('notePhase("")')
    stop = _body(text, STOP_SIG)
    assert 'phase = ""' in stop
    assert "channel?.onSent = nil" in stop


def test_the_relay_says_when_the_mailbox_took_the_request():
    relay = _read(RELAY)
    assert "var onSent: (() -> Void)?" in relay
    assert relay.count("onSent?()") == 1
    send = _body(relay, "private func send(kind: String,")
    assert "onSent?()" in send
    assert send.index("guard code == 200") < send.index("onSent?()")
    assert send.index("onSent?()") < send.index(
        "let deadline = Date().addingTimeInterval(timeout)")
    # The background path words nothing and therefore hooks nothing.
    assert "onSent" not in _read(BACKGROUND)
    background = _body(_read(CLIENT), "func backgroundRefresh(")
    assert "onSent" not in background


def test_the_screens_draw_the_phase_and_hold_no_copy():
    brand = _read(BRAND)
    for name in ("ConnectingView", "ReconnectBar"):
        assert 'var phase: String = ""' in _struct(brand, name), name
    assert brand.count('var phase: String = ""') == 2
    assert _read(APP).count("phase: client.phase") == 2
    for literal in _phase_strings(_read(CLIENT)):
        assert literal not in brand, literal
    bar = _struct(brand, "ReconnectBar")
    # The countdown row is exactly today's once the attempt has ended.
    assert '"retrying now"' in bar
    assert "Text(cells(remaining: left))" in bar
    assert "phase.isEmpty" in bar
    view = _struct(brand, "ConnectingView")
    assert "Text(phase)" in view
    assert "phase: phase" in view


def test_a_cold_launch_reads_the_stored_route():
    text = _read(CLIENT)
    start = _body(text, START_SIG)
    assert "seedRouteFromLastPoll()" in start
    assert start.index("seedRouteFromLastPoll()") < start.index(
        "status = .connecting")
    seed = _body(text, SEED_SIG)
    assert "UserDefaults.standard.bool(forKey: Self.lastKnownAwayKey)" in seed
    assert "guard channel != nil" in seed
    assert "via = .relay" in seed
    assert "via = .lan" not in seed
    # The definition, `start()`, and the lock-screen write's fresh client.
    assert text.count("seedRouteFromLastPoll()") == 3


def test_the_cadence_is_still_untouched():
    test_the_poll_cadence_is_unchanged()
    text = _read(CLIENT)
    code = "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("//"))
    assert "jitter" not in code
    assert "backoff" not in code


# --- nothing else gains work --------------------------------------------------


def test_the_background_path_gains_nothing():
    background = _read(BACKGROUND)
    for token in ("LinkAttempt", "link.", "noteAttempt", "phase"):
        assert token not in background, token


def test_no_new_theme_token():
    """The reconnect bar adds no private color outside generated Signal."""
    token = re.compile(r"static let (\w+) = Color\(")
    phone = set(token.findall(_read(ROOT / "ios" / "BobPhone" / "SignalTokens.generated.swift")))
    panel = set(token.findall(
        _read(ROOT / "panel" / "Sources" / "BobPanel" / "SignalTokens.generated.swift")))
    assert phone, "no colour tokens parsed; the needle has rotted"
    assert phone == panel, phone ^ panel
    assert phone == {
        "canvas", "surface", "raised", "well", "text", "muted", "accent",
        "accentInk", "line", "control", "attention", "danger",
    }, "the generated Signal palette changed without revisiting reconnect"
