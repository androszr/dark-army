# host/tests/test_panel_stream_gate.py
"""The panel's live stream survives a brief cover — source pins over the Swift.

`plans/2026-09-23-panel-stream-survives-brief-covers.md`. The rules
themselves are tabled in Swift: the grace against an injected clock
(`OcclusionGraceTests.swift`), the hidden pane's holding buffer
(`HiddenTerminalBufferTests.swift`) and the off-main decode
(`SnapshotStreamTests.swift`). What this file pins is the wiring those tests
cannot see: that the occlusion writer asks the grace, that every explicit
path cancels it before writing the gate, that neither the open nor the
reconnect reads `/api/state` beside the attach frame, and that a hidden
terminal pane holds its bytes and lets its heartbeat sleep.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
MAIN = PANEL / "main.swift"
CLIENT = PANEL / "DaemonClient.swift"
PANE = PANEL / "TerminalPane.swift"
GRACE = PANEL / "OcclusionGrace.swift"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    text = path.read_text()
    assert text.strip(), f"empty file: {path}"
    return text


def _code(text: str) -> str:
    """`text` with its comments removed, so a pin judges code and not prose."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def _block(text: str, start: str) -> str:
    """The braces-balanced block that opens at the first `start`."""
    assert start in text, f"missing block: {start!r}"
    i = text.index(start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                block = text[i : j + 1]
                assert len(block) > len(start), f"empty block: {start!r}"
                return block
    raise AssertionError(f"unbalanced block at {start!r}")


def test_the_grace_is_four_seconds_and_foundation_only():
    text = _read(GRACE)
    assert "static let seconds: TimeInterval = 4" in text
    imports = [line for line in text.splitlines() if line.startswith("import ")]
    assert imports == ["import Foundation"], imports


def test_the_occlusion_writer_asks_the_grace():
    """The covered edge arms a wait instead of writing the gate; the switcher
    release stays immediate and verbatim above it."""
    code = _code(_read(MAIN))
    publish = _block(code, "private func publishSeen() {")
    assert "grace.observe(" in publish
    assert 'if !seen { releaseSwitcherFocus(reason: "occluded") }' in publish
    assert publish.index("releaseSwitcherFocus") < publish.index("grace.observe(")
    # The wait's timer runs in `.common` mode, or a menu starves it.
    assert "RunLoop.main.add(timer, forMode: .common)" in publish
    # By interval (uptime), and its fire is authoritative: a wall-clock
    # comparison could leave a wait armed with no timer, and the gate would
    # never close again.
    assert "Timer(timeInterval: OcclusionGrace.seconds" in publish
    assert "Timer(fire:" not in publish
    assert "self.grace.expired()" in publish
    assert "fired(now:" not in _code(_read(GRACE))
    # The heartbeat learns of the cover at the cover, not at the settle.
    assert "GraceCover.shared.note(verdict)" in publish
    # The gate is never written with the edge's own value any more: only the
    # `.open` branch writes true, and the fire recomputes.
    assert "client.visible = seen" not in publish


def test_the_fire_recomputes_the_union_rather_than_trusting_the_edge():
    code = _code(_read(MAIN))
    settle = _block(code, "private func settleAfterGrace() {")
    assert "let seen = seenNow()" in settle
    assert "client.visible = seen" in settle
    assert "client.boardOpen = seen" in settle
    # The cover clears after the gate is written, never before.
    assert settle.index("client.visible = seen") < settle.index("GraceCover.shared.clear()")
    union = _block(code, "private func seenNow() -> Bool {")
    for window in ("panel?.occlusionState", "cardWindow?", "settingsWindow?",
                   "knowledgeWindow?", "accessLogWindow?"):
        assert window in union, window


def test_every_explicit_path_cancels_the_grace_before_writing_the_gate():
    code = _code(_read(MAIN))
    for start in ("func hide(yieldToRemembered: Bool = true) {",
                  "func windowWillClose(_ notification: System.Notification) {",
                  "func show(x: Double?, y: Double?) {"):
        body = _block(code, start)
        assert "cancelGrace()" in body, start
        assert body.index("cancelGrace()") < body.index("client.visible ="), start
    cancel = _block(code, "private func cancelGrace() {")
    assert "graceTimer?.invalidate()" in cancel
    assert "grace.cancel()" in cancel
    assert "GraceCover.shared.clear()" in cancel


def test_the_attach_frame_is_the_fetch():
    """Neither the open nor the reconnect reads `/api/state` beside the
    stream's own attach frame; the usage poll stays."""
    main = _code(_read(MAIN))
    show = _block(main, "func show(x: Double?, y: Double?) {")
    assert "client.refresh()" not in show
    assert "client.refreshUsage()" in show
    client = _code(_read(CLIENT))
    read = _block(client, "private func readEvents() async {")
    assert "await refresh()" not in read
    # The usage read rides beside the attach, never ahead of it: the attach
    # frame is the only fleet read, and must not wait behind `/api/usage`.
    attach = read.index('request("/api/events')
    assert "refreshUsage()" in read
    assert "await refreshUsage()" not in read[:attach]
    assert "Task {" in read[: read.index("refreshUsage()")]


def test_the_decode_runs_off_the_main_actor_on_both_paths():
    client = _code(_read(CLIENT))
    assert re.search(r"nonisolated static func prepareFrame\([^)]*\)\s*async\s*->", client)
    assert "private func decode(" not in client
    refresh = _block(client, "func refresh() async {")
    assert "await Self.prepareFrame(" in refresh
    assert "apply(prepared.snapshot" in refresh
    # A stream frame that landed during the off-actor decode is newer: the
    # read is dropped, checked after the decode and before the apply.
    overtaken = refresh.index("Self.overtaken(before: last, now: lastAppliedPayload)")
    assert refresh.index("await Self.prepareFrame(") < overtaken < refresh.index("apply(prepared.snapshot")
    # The baseline is read before the request goes out: a frame applied
    # while the GET was in flight is newer than its answer too.
    assert refresh.index("let last = lastAppliedPayload") < refresh.index("URLSession.shared")
    stream = _block(client, "nonisolated private func consumeStream(")
    assert "await Self.prepareFrame(" in stream


def test_the_heartbeat_sleeps_while_hidden():
    pane = _code(_read(PANE))
    beat = _block(pane, "private func keepFocusStated() async {")
    assert "$visible.values" in beat
    assert beat.count("Task.sleep") == 1
    visible_branch = _block(beat, "if client.visible {")
    assert "Task.sleep" in visible_branch
    assert 'Panel.send(action: "panel_terminal", value: agent.sessionId)' in visible_branch


def test_a_hidden_pane_holds_its_bytes_and_replays_them_on_the_visible_edge():
    pane = _code(_read(PANE))
    assert "static let capBytes = 1 << 20" in pane
    on_data = _block(pane, "conn.onData = {")
    assert "hidden.hold(" in on_data
    assert "self.client.visible" in on_data
    release = _block(pane, "private func releaseHidden() {")
    assert "hidden.drain()" in release
    assert "open()" in release
    # A new connection starts from its own paint: the buffer is reset first.
    opener = _block(pane, "private func open() {")
    assert opener.index("hidden = HiddenTerminalBuffer()") < opener.index("generation += 1")
    shutdown = _block(pane, "func shutdown() {")
    assert "visibleWatch?.cancel()" in shutdown
    watch = _block(pane, "func startFocusWatch() {")
    assert "$visible" in watch and "releaseHidden()" in watch


def test_a_covered_pane_stops_naming_its_session_during_the_grace():
    """The stream stays open through the grace, so `client.visible` stays
    true; the heartbeat must not, or the daemon's alert suppression trusts a
    covered pane for four more seconds. `GraceCover` is not a second flag on
    `DaemonClient`."""
    pane = _code(_read(PANE))
    beat = _block(pane, "private func keepFocusStated() async {")
    assert beat.index("if cover.inGrace {") < beat.index("if client.visible {")
    assert "cover.$inGrace.values" in beat
    assert ".onChange(of: cover.inGrace)" in pane
    assert "GraceCover.stated(" in _block(pane, ".onChange(of: cover.inGrace)")
    assert "GraceCover.stated(" in _block(pane, ".onChange(of: client.visible)")
    stated = _block(pane, "nonisolated static func stated(")
    assert "visible && !inGrace ? session : \"\"" in stated
    client = _code(_read(CLIENT))
    assert "GraceCover" not in client and "inGrace" not in client
