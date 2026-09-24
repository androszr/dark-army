# host/tests/test_phone_terminal.py
"""The phone's terminal — source pins over the Swift.

`ios/` has no test target by decision; this suite reads the Swift as text.
What it pins after the port to a real emulator: the two row flags and the
two board markers decode with a **false** default; the away feed's frame
(`TerminalBytesFrame`) decodes every key tolerantly with `painted` and
`dataMore` false; the client asks for raw bytes (`grid=0`, a `since_bytes`
cursor) on the **relay leg only**, one hop per poll and never a `more`
loop, guarded on being away and on the stream marker; the pane is
SwiftTerm fed straight from the socket at home (`SealedTerminalStream`)
and from the poll away, its coordinator the panel's reconnect pattern with
no `@State`; keys go up the stream at home and ride `terminal_input` with
`bytes` away; the grid renderer, its palette and the one-line send box are
gone; `AnswerBox`'s free-text reply still yields to the terminal; the
widget and the background check-in never touch it; the panel's pane is
untouched. The drift pin between the two `TerminalStream.swift` copies is
`test_phone_terminal_stream_drift.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
MODELS = PHONE / "Models.swift"
CLIENT = PHONE / "Client.swift"
DETAIL = PHONE / "AgentDetailView.swift"
PANE = PHONE / "TerminalPane.swift"
STREAM = PHONE / "TerminalStream.swift"
HOME = PHONE / "HomeTransport.swift"
RELAY = PHONE / "RelayTransport.swift"
ACTIONS = PHONE / "Actions.swift"
REFRESH = PHONE / "BackgroundRefresh.swift"
WIDGET_DIR = ROOT / "ios" / "BobPhoneWidget"
PANEL = ROOT / "panel" / "Sources" / "BobPanel"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _code(text: str) -> str:
    """`text` with its comments removed, so a pin judges code and not prose.

    A doc comment is allowed to name the construct it promises the file does
    not use; only a line of Swift may not.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def _block(text: str, start: str) -> str:
    """The braces-balanced block that opens at the first `start`."""
    i = text.index(start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    raise AssertionError(f"unbalanced block at {start!r}")


# --- decode -------------------------------------------------------------------


def test_the_row_flags_decode_false_by_default():
    src = _read(MODELS)
    assert 'case ownTerminal = "own_terminal"' in src
    assert 'case canTerminalInput = "can_terminal_input"' in src
    assert "ownTerminal = c.value(.ownTerminal, false)" in src
    assert "canTerminalInput = c.value(.canTerminalInput, false)" in src


def test_the_board_markers_decode_false_by_default():
    src = _read(MODELS)
    assert 'case terminalSupported = "terminal_supported"' in src
    assert "terminalSupported = c.value(.terminalSupported, false)" in src
    assert 'case terminalStreamSupported = "terminal_stream_supported"' in src
    assert "terminalStreamSupported = c.value(.terminalStreamSupported, false)" in src


def test_the_bytes_frame_decodes_every_key_tolerantly():
    frame = _block(_read(MODELS), "struct TerminalBytesFrame")
    for key in ("available", "session", "data", "bytesRead", "painted", "dataMore",
                "ringOverflowed", "cols", "rows", "exited", "reason"):
        assert f"{key} = c.value(.{key}," in frame, key
    assert 'case bytesRead = "bytes_read"' in frame
    assert 'case dataMore = "data_more"' in frame
    assert 'case ringOverflowed = "ring_overflowed"' in frame
    # A frame that does not say so is not a reset and has nothing more.
    assert "painted = c.value(.painted, false)" in frame
    assert "dataMore = c.value(.dataMore, false)" in frame
    assert "Data(base64Encoded: data) ?? Data()" in frame


def test_the_grid_renderer_and_its_types_are_gone():
    for name in ("TerminalRun", "TerminalRowChange", "TerminalFrame", "TerminalScreen"):
        assert f"struct {name}" not in _read(MODELS), name
    for path in PHONE.glob("*.swift"):
        code = _code(path.read_text())
        for gone in ("PhoneTerminalView", "TerminalScreen", "PhoneTerminalPalette",
                     "struct InputBar", "struct TerminalRun", "cursorAnchor",
                     "followCursor"):
            assert gone not in code, f"{path.name} still names {gone}"


# --- the client ---------------------------------------------------------------


def test_the_away_feed_is_asked_for_on_the_relay_leg_only():
    src = _read(CLIENT)
    assert src.count("func fetchTerminalBytes(") == 1
    assert src.count("await fetchTerminalBytes(") == 1
    assert "fetchTerminal(" not in _code(src)
    relay_leg = _block(src, "private func pollViaRelay(")
    assert "await fetchTerminalBytes()" in relay_leg
    home_leg = _block(src, "private func pollDirect(")
    assert "fetchTerminal" not in home_leg


def test_the_away_feed_is_gated_asks_for_raw_bytes_and_never_loops():
    fetch = _block(_read(CLIENT), "func fetchTerminalBytes(")
    assert "let sessionId = terminalWatching" in fetch
    assert "!backgroundRun" in fetch
    assert "!terminalUnsupported" in fetch
    assert "knowsItIsAway" in fetch
    assert "snapshot.board.terminalStreamSupported" in fetch
    assert "onTerminalBytes != nil" in fetch
    assert "grid=0&since_bytes=\\(terminalCursor)" in fetch
    assert 'kind: "terminal"' in fetch
    assert "while hops" not in fetch
    # The phone's own grid rides the query (a phone resize on the Mac,
    # `terminal_phone_resize`), never a `resize` the panel owns.
    assert "&cols=\\(size.cols)&rows=\\(size.rows)" in fetch
    assert "terminalPhoneSize" in fetch
    assert "terminalCursor = frame.bytesRead" in fetch
    assert "onTerminalBytes?(frame)" in fetch


def test_a_404_latches_and_a_fresh_start_clears_it():
    src = _read(CLIENT)
    fetch = _block(src, "func fetchTerminalBytes(")
    assert "if answer.status == 404 { terminalUnsupported = true }" in fetch
    start = _block(src, "func start(record: PairingRecord)")
    assert "terminalUnsupported = false" in start


def test_watching_resets_the_cursor_so_the_first_frame_is_a_paint():
    src = _read(CLIENT)
    assert "private(set) var terminalCursor = -1" in src
    watch = _block(src, "func watchTerminal(")
    assert "terminalCursor = -1" in watch
    restart = _block(src, "func restartTerminalFeed(")
    assert "terminalCursor = -1" in restart
    assert "var knowsItIsAway: Bool" in src
    assert "private var knowsItIsAway" not in src


def test_the_stream_is_asked_of_the_home_channel_and_a_403_forgets_the_pairing():
    src = _read(CLIENT)
    stream = _block(src, "func terminalStream(session: String)")
    assert "home.openTerminalStream(session: session, host: record.host" in stream
    refused = _block(src, "func noteTerminalStreamRefused()")
    assert "forgetPairing()" in refused
    # The client builds no stream address of its own.
    assert "/api/terminal/stream" not in src
    assert "SealedTerminalStream(" not in src


def test_the_background_check_in_and_the_widget_never_fetch_it():
    refresh = _read(REFRESH)
    assert "fetchTerminal" not in refresh
    assert "watchTerminal" not in refresh
    for path in WIDGET_DIR.rglob("*.swift"):
        assert "terminal" not in path.read_text().lower(), path


# --- the stream ---------------------------------------------------------------


def test_the_opening_frame_is_sealed_in_home_transport_and_nowhere_else():
    home = _read(HOME)
    opener = _block(home, "func openTerminalStream(session: String")
    assert 'kind: "terminal_stream"' in opener
    assert "sendCtr += 1" in opener
    assert "HostAddress.check(host: host, typedPort: port)" in opener
    assert "SealedTerminalStream(host: cleaned.host, port: cleaned.port" in opener
    assert "onCounterExpected" in opener
    assert "func fastForward(to expected: Int)" in home
    for path in PHONE.glob("*.swift"):
        if path.name in ("TerminalStream.swift",):
            continue
        assert "/api/terminal/stream" not in path.read_text(), path.name
    stream = _read(STREAM)
    assert 'contentType = "application/x-bob-terminal-sealed"' in stream
    assert "POST /api/terminal/stream HTTP/1.1" in stream
    assert "X-Bob-Channel:" in stream
    assert "X-Bob-Token" not in stream


def test_every_frame_after_the_head_is_sealed_and_bound_to_its_position():
    stream = _read(STREAM)
    conn = _block(stream, "final class SealedTerminalStream")
    assert 'UInt8(ascii: "Z")' in conn
    assert 'frameId: "\\(frameId)|p2m|\\(n)"' in conn
    assert 'frameId: "\\(frameId)|m2p|\\(downCount)"' in conn
    assert "RelayTransport.sealBlob(" in conn
    assert "RelayTransport.openBlob(" in conn
    assert "ns: .home" in conn
    # A frame that does not open ends the stream; the exit never leaks.
    ingest = _block(conn, "private func ingest(_ data: Data)")
    assert "finish()" in ingest
    assert "heartbeatSeconds: TimeInterval = 5" in conn
    assert "onCounterExpected" in conn
    relay = _read(RELAY)
    assert "static func openBlob(key: Data, direction: RelayDirection, frameId: String" in relay


# --- the pane -----------------------------------------------------------------


def test_the_pane_is_swiftterm_fed_from_the_socket_with_no_state():
    pane = _read(PANE)
    assert pane.startswith("import SwiftTerm\n") or "\nimport SwiftTerm\n" in pane
    host = _block(pane, "struct PhoneTerminalHost")
    assert "UIViewRepresentable" in host
    coordinator = _block(host, "final class Coordinator")
    assert "@State" not in coordinator
    assert "setup(isReset: true)" in coordinator
    assert "generation" in coordinator
    assert "reconnectDelay" in coordinator and "refusedDelay" in coordinator
    assert "resizeDebounce" in coordinator
    assert "feed(byteArray:" in coordinator
    attach = _block(coordinator, "func attach(session: String)")
    assert "setup(isReset: true)" in attach
    assert attach.index("setup(isReset: true)") < attach.index("open()")
    # Feed straight into the view; nothing goes through a published value.
    assert "@Published" not in pane
    assert "allowMouseReporting = false" in pane
    assert "installColors(" in pane
    assert "0x1f1f1f" in pane and "0xcccccc" in pane
    assert "ProgressView" not in pane
    assert "GeometryReader" not in pane
    assert ".lineLimit(" not in pane


def test_keys_go_up_the_stream_at_home_and_ride_bytes_away():
    coordinator = _block(_block(_read(PANE), "struct PhoneTerminalHost"), "final class Coordinator")
    send = _block(coordinator, "func send(source: TerminalView, data: ArraySlice<UInt8>)")
    assert "stream.send(input: Data(data))" in send
    away = _block(coordinator, "private func flushAway()")
    assert "PhoneActions.terminalInput" in away
    assert '"bytes": payload.base64EncodedString()' in away
    assert "client.post(" in away
    assert "quietPost" not in coordinator
    assert '"text"' not in coordinator


def test_the_away_feed_resets_on_a_paint_and_the_stream_pauses_off_screen():
    coordinator = _block(_block(_read(PANE), "struct PhoneTerminalHost"), "final class Coordinator")
    polled = _block(coordinator, "private func openPolled(_ gen: Int)")
    assert "client.onTerminalBytes = {" in polled
    assert "if frame.painted {" in polled
    assert "setup(isReset: true)" in polled
    assert "client.restartTerminalFeed()" in polled
    pause = _block(coordinator, "private func pause()")
    assert "stream?.cancel()" in pause
    assert "client.onTerminalBytes = nil" in pause
    pane = _read(PANE)
    assert "@Environment(\\.scenePhase)" in pane
    # `.inactive` is a Control Centre pull, a banner or the away Face ID
    # sheet — none of them a reason to tear the socket down and pay a fresh
    # `_home_open`, an away lease and a whole repaint. Only `.background`
    # is iOS taking the socket away.
    assert "active: scenePhase != .background" in pane
    assert "scenePhase == .active" not in pane
    assert "away: client.knowsItIsAway" in pane


def test_a_stream_that_cannot_open_says_so_and_stops_asking():
    """A retry every second behind a header reading `live` is a black
    rectangle a person cannot tell from an agent that has printed nothing."""
    coordinator = _block(_block(_read(PANE), "struct PhoneTerminalHost"),
                         "final class Coordinator")
    reconnect = _block(coordinator, "private func scheduleReconnect()")
    assert "attempts += 1" in reconnect
    assert "Self.maxAttempts" in reconnect
    assert "gaveUp" in reconnect
    # The wait doubles and is capped; the pane says what is happening.
    assert "pow(2, Double(attempts - 1))" in reconnect
    assert "Self.maxReconnectDelay" in reconnect
    assert "onNote(Self.reconnectingNote)" in reconnect
    assert "onNote(Self.unreachableNote)" in reconnect
    # A **final** 409 is "I do not host this session": nothing to wait for.
    # The door answers 409 for two transient things as well — a stale
    # counter and a clock too far out — and both are routine on open, so
    # latching on the bare status stranded the pane offline on a race that
    # had already healed. `re` is what tells them apart: the attach refusal
    # names the stream, the transient two carry `""`.
    refused = coordinator[coordinator.index("conn.onRefused = {"):]
    assert "if status == 409 && isFinal { self.gaveUp = true }" in refused
    assert "if status == 409 && !isFinal {" in refused
    stream = _read(STREAM)
    assert "var onRefused: ((Int, String, Bool) -> Void)?" in stream
    assert 'isFinal = (payload["re"] as? String ?? "") == frameId' in stream
    # And the header never says `live` about a line that is not up.
    view = _block(_read(PANE), "struct PhoneTerminalPane")
    assert 'return linked ? "live" : "offline"' in view
    assert "onLink: { linked = $0 }" in view


def test_the_size_readout_is_observed_state_and_not_a_reference():
    """`size.cols = c` on a class held in `@State` mutates nothing SwiftUI
    watches, and that readout is exactly what the width rule is judged by."""
    pane = _read(PANE)
    view = _block(pane, "struct PhoneTerminalPane")
    assert "@State private var cols = 0" in view
    assert "@State private var rows = 0" in view
    assert "PhoneTerminalSize" not in pane


def test_a_key_typed_before_the_head_lands_is_held_not_swallowed():
    """`send(source:)` sees a non-nil stream from the moment it is made, so
    the silence was inside the stream: `sealedWrite` bailed on `headDone`."""
    conn = _block(_read(STREAM), "final class SealedTerminalStream")
    send = _block(conn, "func send(input: Data)")
    assert "pendingInput" in send
    assert "onInputDropped?()" in send
    assert "maxPendingInput" in send
    assert "flushPendingInput()" in conn
    finish = _block(conn, "private func finish()")
    assert "onInputDropped?()" in finish
    coordinator = _block(_block(_read(PANE), "struct PhoneTerminalHost"),
                         "final class Coordinator")
    assert "conn.onInputDropped = {" in coordinator
    assert "onNote(Self.keyLostNote)" in coordinator


def test_an_older_mac_gets_one_sentence_and_no_emulator():
    pane = _read(PANE)
    view = _block(pane, "struct PhoneTerminalPane")
    assert "if !client.snapshot.board.terminalStreamSupported" in view
    assert 'olderMacSentence = "This Mac\'s Dark Army is too old to stream a terminal."' in view
    branch = view[view.index("if !client.snapshot.board.terminalStreamSupported"):
                  view.index("} else {")]
    assert "PhoneTerminalHost(" not in branch
    assert "Text(Self.olderMacSentence)" in branch


def test_the_pane_speaks_as_one_element_and_names_its_size_buttons():
    pane = _read(PANE)
    view = _block(pane, "struct PhoneTerminalPane")
    assert ".accessibilityElement(children: .ignore)" in view
    assert ".accessibilityLabel(spoken)" in view
    assert 'accessibilityLabel("Smaller terminal text")' in view
    assert 'accessibilityLabel("Larger terminal text")' in view
    assert '@AppStorage("terminalFontSize")' in view
    speak = _block(_block(pane, "final class Coordinator"), "private func speak()")
    assert "getLine(row: row)" in speak
    assert "translateToString(trimRight: true)" in speak
    assert '"Terminal. "' in speak


# --- the screen ---------------------------------------------------------------


def test_a_hosted_row_draws_the_pane_outside_the_scroll_view():
    detail = _read(DETAIL)
    gate = _block(detail, "private var hostedTerminal: Bool")
    assert "agent.ownTerminal && client.snapshot.board.terminalSupported" in gate
    hosted = _block(detail, "var body: some View")
    assert "PhoneTerminalPane(" not in hosted
    assert "PhoneAgentScreenBar(screen: $screen" in hosted
    cover = _block(detail, "private var terminalCover: Binding<Bool>")
    assert "DetailTab.terminalAttached(hosted: hostedTerminal, tab: hostedTab)" in cover
    screen = _block(detail, "private var terminalScreen: some View")
    assert "PhoneTerminalPane(agent: agent, client: client)" in screen
    # Full-screen terminal stays a sibling of the strip, outside its scroll.
    strip = _block(screen, "ScrollView {")
    assert "PhoneTerminalPane" not in strip
    assert screen.index("ScrollView {") < screen.index("PhoneTerminalPane(")
    assert ".safeAreaInset(" not in detail
    assert "ScrollViewReader" not in detail
    assert "DragGesture(" not in detail


def test_the_strip_above_the_terminal_is_folded_by_default():
    """The tab is for the screen. With the emulator's keyboard up, an
    always-open strip of controls left the terminal a sliver under its own
    header (seen 2026-09-12); now the strip is one line until a tap or an
    arriving ask opens it, and the opened strip is still bounded."""
    detail = _read(DETAIL)
    assert "@State private var stripOpen = false" in detail
    screen = _code(_block(detail, "private var terminalScreen: some View"))
    assert "if stripOpen {" in screen
    strip = _block(screen, "if stripOpen {")
    assert "ScrollView {" in strip
    assert "PhoneTerminalPane" not in strip
    assert "Self.hostedStripMaxHeight" in strip
    # Done and the fold share one row: the terminal pays for neither twice.
    assert screen.index('Text("Done")') < screen.index("stripOpen.toggle()")
    assert "TerminalStrip.foldLabel(open: stripOpen" in screen
    # An ask opens it by itself; nothing else does.
    assert ".onChange(of: TerminalStrip.needsPress(" in screen
    assert "if needs { stripOpen = true }" in screen
    assert screen.count("stripOpen = true") == 1
    rules = _code(_block(detail, "enum TerminalStrip"))
    assert "static func needsPress(prompts: Int, questions: Int) -> Bool" in rules
    assert "prompts > 0 || questions > 0" in rules
    assert "static func foldLabel(open: Bool, prompts: Int, questions: Int) -> String" in rules
    # The opened strip may never take the screen back.
    assert "static let hostedStripMaxHeight: CGFloat = 200" in detail
    assert "static let hostedStripMaxHeightLarge: CGFloat = 320" in detail
    tests = _read(PHONE.parent / "BobPhoneTests" / "TerminalFramingTests.swift")
    assert "final class TerminalStripTests" in tests
    assert "TerminalStrip.needsPress(prompts: 1, questions: 0)" in tests


def test_the_reply_box_still_yields_to_the_terminal():
    detail = _read(DETAIL)
    gate = _block(detail, "private var terminalInputHere")
    assert "agent.ownTerminal && agent.canTerminalInput" in gate
    assert "client.snapshot.board.terminalStreamSupported" in gate
    assert "terminalWins: terminalInputHere" in detail
    box = _read(PHONE / "AnswerBox.swift")
    assert "terminalWins: Bool = false" in box
    assert "&& !terminalWins" in box
    needs_you = _read(PHONE / "NeedsYouView.swift")
    assert "AnswerBox(" not in needs_you
    assert "terminalWins" not in needs_you


def test_the_terminal_tab_draws_no_second_copy_of_the_message():
    """The last message and the live screen are two tabs now, never one
    above the other. So `body(for:)` no longer special-cases a hosted row —
    it is drawn on the Details tab, where there is no terminal on screen to
    duplicate — and `hostedBody` itself draws no message document."""
    detail = _read(DETAIL)
    body = _block(detail, "private func body(for agent: Agent)")
    assert "agent.ownTerminal" not in body
    hosted = _code(_block(detail, "var body: some View"))
    assert "body(for: agent)" not in hosted
    details = _code(_block(detail, "private var detailsScreen: some View"))
    assert "rest" in details
    rest = _code(_block(detail, "private var rest: some View"))
    assert "body(for: agent)" in rest
    terminal = _code(_block(detail, "private var terminalScreen: some View"))
    assert "body(for: agent)" not in terminal
    # The still and the name lead the sheet above the tabs now, shared by
    # both panes, so this scroll starts at the question. It is not a second
    # copy of the facts (`elsewhereTerminal` stays in `rest`).
    assert "identity" not in details
    assert hosted.index("identity") < hosted.index("PhoneAgentScreenBar(")
    assert "elsewhereTerminal" not in details


def test_watching_starts_on_appear_and_stops_on_disappear():
    detail = _read(DETAIL)
    assert "client.watchTerminal(agent.sessionId)" in detail
    assert "client.watchTerminal(nil)" in detail
    # Both call sites are one helper now, and the helper asks the tab.
    watch = _code(_block(detail, "private func syncWatch()"))
    assert "DetailTab.terminalAttached(hosted: hostedTerminal, tab: hostedTab)" in watch
    assert "client.watchTerminal(agent.sessionId)" in watch
    assert "client.watchTerminal(nil)" in watch
    # The cover reasserts the watch when UIKit hides its presenting view.
    assert detail.count("syncWatch()") == 5
    cover = _code(_block(detail, ".fullScreenCover(isPresented: terminalCover)"))
    assert ".onAppear { sheets.terminalPresented = true; syncWatch() }" in cover
    assert "sheets.terminalPresented = false" in cover
    assert "client.watchTerminal(nil)" in cover
    pane = _read(PHONE / "TerminalPane.swift")
    assert "static let keyboardDelay: TimeInterval = 0.45" in pane
    assert "func claimKeyboard()" in pane
    assert "view?.resignFirstResponder()" in pane
    assert "if !terminalCover.wrappedValue { client.watchTerminal(nil) }" in detail
    assert ".onChange(of: hostedTab)" in detail


def test_the_action_name_survives():
    actions = _read(ACTIONS)
    assert 'static let terminalInput = "terminal_input"' in actions
    assert "`bytes`" in actions
    post = _block(_read(CLIENT), "func post(action: String")
    assert 'body["command_token"] = mark' in post


def test_jump_to_a_hosted_session_opens_the_agent_not_the_card():
    """The live screen is the agent. A pipeline row or a card whose
    session Dark Army hosts must open that agent sheet, never the card sheet."""
    models = _read(MODELS)
    helper = _block(models, "func hostedAgent(for card: BoardCard)")
    assert "agent.ownTerminal" in helper
    assert "card.refineSessionId" in helper
    pipeline = _read(PHONE / "PipelineView.swift")
    dest = _block(pipeline, "private func rowSheet(_ card: BoardCard)")
    assert "hostedAgent(for: card)" in dest
    assert "return .agent(agent, category)" in dest
    assert "return .card(card)" in dest
    assert dest.index("return .agent") < dest.index("return .card")
    assert "sheets.show(rowSheet(card))" in pipeline
    card = _read(PHONE / "CardDetailView.swift")
    link = _block(card, "private var terminalLink")
    assert "hostedAgent(for: card)" in link
    assert "sheets.show(.agent(agent, category))" in link
    assert 'accessibilityLabel("Open this session\'s terminal")' in link
    assert "OPEN TERMINAL" in link


def test_the_contract_states_the_terminal_rule():
    contract = _read(ROOT / "docs" / "phone-contract.md")
    assert "## The terminal is a real emulator, and it is the one exception" in contract
    assert "## The terminal wraps and has one send box" not in contract
    assert "restating a detent sheet under a" in contract
    assert "first-responder full-screen cover is the crash" in contract
    transport = _read(ROOT / "docs" / "transport-contract.md")
    assert "## The terminal stream is a sealed held-open route" in transport


# --- the panel's half ---------------------------------------------------------


def test_the_panel_decodes_the_same_two_flags_false_by_default():
    src = _read(PANEL / "Models.swift")
    assert "ownTerminal = c.value(.ownTerminal, false)" in src
    assert "canTerminalInput = c.value(.canTerminalInput, false)" in src


def test_the_panel_pane_is_untouched():
    pane = _read(PANEL / "TerminalPane.swift")
    assert "GeometryReader" not in _code(pane)
    assert 'Panel.send(action: "panel_terminal"' in pane
    assert "ptySize.cols" in pane
    fetch = _read(PANEL / "Fetchers.swift")
    assert 'request("/api/terminal?session=' in fetch
    assert '"action": "terminal_input"' in fetch
    assert "Authorization" not in fetch


def test_the_panel_pane_drops_the_previous_rows_screen_before_attaching():
    attach = _block(_read(PANEL / "TerminalPane.swift"), "func attach(session: String)")
    assert "setup(isReset: true)" in attach
    assert "exited = false" in attach
    assert attach.index("setup(isReset: true)") < attach.index("open()")


def test_both_clients_depend_only_on_swiftterm():
    """SwiftTerm is the one package either client is allowed."""
    manifest = _read(ROOT / "panel" / "Package.swift")
    assert "SwiftTerm" in manifest
    assert manifest.count(".package(") == 1


# --- typing from away (21 Sep 2026) ---------------------------------------------


def test_a_landed_key_batch_asks_for_its_echo_at_once():
    """Keys from away landed on the pty the moment the 200 came back, but
    the screen learned it only on the next check-in's terminal leg — up to
    eight seconds of pause plus three relay legs — which read as a terminal
    that takes no input. The flush now asks for the feed itself, after a
    send that landed and never after a refusal. The call is the pane's;
    `Client.swift` keeps its one call site in `pollViaRelay`, which
    `test_the_away_feed_is_asked_for_on_the_relay_leg_only` pins."""
    coordinator = _block(_block(_read(PANE), "struct PhoneTerminalHost"),
                         "final class Coordinator")
    flush = _block(coordinator, "private func flushAway()")
    assert "if result.ok { await client.fetchTerminalBytes() }" in flush
    assert _code(flush).count("fetchTerminalBytes()") == 1
    # The echo is asked for before the next batch is let through.
    assert flush.index("fetchTerminalBytes()") < flush.index("self.flushAway()")


def test_the_away_hint_and_the_note_sit_under_the_grid_not_over_it():
    """As a bottom overlay they covered the last rows — with the keyboard
    up, the prompt line — so a person typing from away watched their own
    keys vanish behind the sentence explaining where keys go."""
    pane = _read(PANE)
    code = _code(pane)
    assert ".overlay(alignment: .bottom)" not in code
    assert "Text(AwayKeys.hint)" in code
    assert "Text(note)" in code
    host = code.index("PhoneTerminalHost(")
    hint = code.index("Text(AwayKeys.hint)")
    note = code.index("Text(note)")
    assert host < hint < note
    # Opaque, not translucent: nothing is drawn behind them any more.
    assert ".opacity(0.92)" not in code

