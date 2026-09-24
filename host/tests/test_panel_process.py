# host/tests/test_panel_process.py
"""The panel's stderr, and what the app log makes of it.

The panel has no log of its own — its flight recorder (`Trace.swift`) writes to
stderr, and this is the seam that files those lines in the same log as the
daemon events they are about. That adjacency is the reason the trace exists, so
it is worth a test: routed at the wrong level, the trace either drowns the real
warnings or hides among them.
"""

import io
import json
import threading
import time
from types import SimpleNamespace

import pytest

from dark_army_menubar import panel_process

from dark_army_menubar.panel_process import PanelProcess


class _Proc:
    def __init__(self, *lines):
        self.stderr = io.BytesIO(b"".join(l.encode() + b"\n" for l in lines))


def test_trace_lines_are_information_and_lose_their_marker(caplog):
    with caplog.at_level("INFO", logger="dark-army"):
        PanelProcess._read_errors(_Proc("trace 20:32:48.101 sse open"))
    record = caplog.records[-1]
    assert record.levelname == "INFO"
    # The marker is routing, not content: it has done its job by here.
    assert record.getMessage() == "panel: 20:32:48.101 sse open"


def test_anything_else_on_stderr_is_still_a_warning(caplog):
    with caplog.at_level("INFO", logger="dark-army"):
        PanelProcess._read_errors(_Proc("BobPanel[123]: CoreGraphics complained"))
    record = caplog.records[-1]
    assert record.levelname == "WARNING"
    assert "CoreGraphics" in record.getMessage()


def test_blank_lines_are_dropped(caplog):
    with caplog.at_level("INFO", logger="dark-army"):
        PanelProcess._read_errors(_Proc("", "   ", "trace 1 hidden"))
    assert len(caplog.records) == 1


# ── the commands the menu bar sends ──────────────────────────────────────────

def _sent(**kwargs):
    """Capture the one payload a call writes to the panel's stdin."""
    panel = object.__new__(PanelProcess)
    seen = []
    panel._send = seen.append          # type: ignore[method-assign]
    return panel, seen


def test_show_names_the_session_the_banner_was_about():
    """A tap on a banner is not a toggle: it names a session, and the answer to
    "take me to this agent" is never to close the panel."""
    panel, seen = _sent()
    panel.show((100.0, 20.0, 60.0, 22.0), focus="s1")
    assert seen[0]["action"] == "show"
    assert seen[0]["focus"] == "s1"
    # The whole rect rides along, as it does on a toggle: the panel hangs off
    # the right edge and also has to recognise a click on the item itself.
    assert (seen[0]["x"], seen[0]["y"]) == (160.0, 20.0)
    assert (seen[0]["ax"], seen[0]["aw"]) == (100.0, 60.0)


def test_show_without_a_session_carries_no_focus():
    """An absent key rather than an empty one — the panel reads `focus` as
    "this show was aimed", and "" is not a session."""
    panel, seen = _sent()
    panel.show(None)
    assert seen[0] == {"action": "show"}


def test_toggle_is_unchanged_by_the_focus_path():
    panel, seen = _sent()
    panel.toggle((100.0, 20.0, 60.0, 22.0))
    assert seen[0]["action"] == "toggle"
    assert "focus" not in seen[0]


def test_show_can_also_ask_the_board_to_reveal_the_card():
    """The reverse jump from VS Code aims at the work, so it asks for both
    halves: the row in the rail and the card on the board."""
    panel, seen = _sent()
    panel.show(None, focus="s1", card=True)
    assert seen[0] == {"action": "show", "focus": "s1", "card": True}


def test_the_banner_taps_payload_is_unchanged():
    """`card` is additive and written only when set: a tap on a banner must go
    on aiming the rail alone, and an older panel binary sees the same bytes."""
    panel, seen = _sent()
    panel.show(None, focus="s1")
    assert seen[0] == {"action": "show", "focus": "s1"}


# ── the sender thread: `_send` never blocks the AppKit thread ────────────────

class _Stdin:
    """A stdin whose write can block (a panel that stopped draining) or break
    (a panel that died). `gate` starts open; clear it to wedge the writer."""

    def __init__(self, broken=False):
        self.lines = []
        self.gate = threading.Event()
        self.gate.set()
        self.broken = broken

    def write(self, data):
        if self.broken:
            raise BrokenPipeError
        self.gate.wait()
        self.lines.append(data)

    def flush(self):
        pass


class _FakeProc:
    def __init__(self, stdin):
        self.stdin = stdin
        self.pid = 12345

    def poll(self):
        return None


def _wired(stdin, sender=True):
    """A PanelProcess around a fake, already-'running' panel process."""
    panel = PanelProcess(on_action=None)
    panel._proc = _FakeProc(stdin)
    if sender:
        panel._start_sender()
    return panel


def _payloads(outbox):
    return [json.loads(line) for line, _quit in outbox]


def test_a_blocked_stdin_does_not_block_send():
    """The write lives on the sender thread; `_send` only enqueues. A panel
    wedged mid-sheet must cost queued lines, never the AppKit thread."""
    stdin = _Stdin()
    stdin.gate.clear()                      # the panel stops draining
    panel = _wired(stdin)
    began = time.monotonic()
    for i in range(8):
        assert panel.set_context(n=i)
    assert time.monotonic() - began < 1.0
    stdin.gate.set()                        # it recovers; the lines drain
    deadline = time.monotonic() + 2.0
    while len(stdin.lines) < 8 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [json.loads(l)["n"] for l in stdin.lines] == list(range(8))


def test_overflow_drops_the_oldest_so_the_newest_wins():
    """No sender running, so the queue only fills: past QUEUE_MAX the oldest
    context line is shed — a context push is a snapshot and the newest wins."""
    panel = _wired(_Stdin(), sender=False)
    for i in range(PanelProcess.QUEUE_MAX + 3):
        panel.set_context(n=i)
    queued = _payloads(panel._outbox)
    assert len(queued) == PanelProcess.QUEUE_MAX
    assert queued[0]["n"] == 3              # 0..2 were shed, oldest first
    assert queued[-1]["n"] == PanelProcess.QUEUE_MAX + 2


def test_quit_is_queued_even_when_the_queue_is_full():
    """A dropped `quit` strands a process, so it always gets a place — at the
    cost of the oldest context line, never of another quit."""
    panel = _wired(_Stdin(), sender=False)
    for i in range(PanelProcess.QUEUE_MAX):
        panel.set_context(n=i)
    panel._send({"action": "quit"})
    queued = _payloads(panel._outbox)
    assert len(queued) == PanelProcess.QUEUE_MAX
    assert queued[-1] == {"action": "quit"}
    assert queued[0]["n"] == 1              # the oldest context paid for it


def test_a_broken_pipe_marks_the_process_dead():
    """The sender stands down and clears `_proc`; the next `_send` is what
    spawns a fresh panel — respawning stays on the caller side."""
    panel = _wired(_Stdin(broken=True))
    panel.set_context(n=1)
    deadline = time.monotonic() + 2.0
    while panel._proc is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert panel._proc is None
    assert panel._sender_stop.is_set()


# Isolated launch layouts: never consult an installed panel or start a real one.
@pytest.fixture
def panel_layout(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    checkout = root / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    resources = root / "Dark Army.app" / "Contents" / "Resources"
    monkeypatch.setattr(panel_process.sys, "executable",
                        str(resources.parent / "MacOS" / "Dark Army"))
    monkeypatch.setattr(panel_process, "__file__",
                        str(checkout / "host" / "dark_army_menubar" / "panel_process.py"))
    monkeypatch.setenv("PATH", str(root / "bin"))
    return SimpleNamespace(
        checkout=checkout,
        nested=resources / "BobPanel.app" / "Contents" / "MacOS" / "BobPanel",
        bare=resources / "BobPanel",
        release=checkout / "panel" / ".build" / "release" / "BobPanel",
        debug=checkout / "panel" / ".build" / "debug" / "BobPanel",
        search=root / "bin" / "BobPanel",
        outside=root / "panel" / ".build" / "release" / "BobPanel",
    )


def _panel_file(path, mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("isolated executable fixture")
    path.chmod(mode)
    return str(path)


@pytest.mark.parametrize("frozen", [True, None], ids=["first-launch", "restart"])
@pytest.mark.parametrize("winner", ["nested", "bare", "release", "debug", "search", None])
def test_panel_launch_precedence(panel_layout, monkeypatch, frozen, winner):
    if frozen is None:
        monkeypatch.delattr(panel_process.sys, "frozen", raising=False)
    else:
        monkeypatch.setattr(panel_process.sys, "frozen", frozen, raising=False)
    order = ["nested", "bare", "release", "debug", "search"]
    if winner is not None:
        for name in order[order.index(winner):]:
            _panel_file(getattr(panel_layout, name))
    expected = str(getattr(panel_layout, winner)) if winner else None
    assert panel_process.find_executable() == expected


@pytest.mark.parametrize("unusable", ["missing", "nonexecutable", "directory"])
@pytest.mark.parametrize("candidate,fallback", [("nested", "bare"), ("bare", "release"),
                                                   ("release", "debug")])
def test_unusable_candidates_fall_through(panel_layout, candidate, fallback, unusable):
    rejected = getattr(panel_layout, candidate)
    if unusable == "nonexecutable":
        _panel_file(rejected, 0o644)
    elif unusable == "directory":
        rejected.mkdir(parents=True)
    expected = _panel_file(getattr(panel_layout, fallback))
    assert panel_process.find_executable() == expected


def test_git_boundary_stops_the_ancestor_search(panel_layout):
    _panel_file(panel_layout.outside)
    assert panel_process.find_executable() is None
    expected = _panel_file(panel_layout.search)
    assert panel_process.find_executable() == expected


def test_ancestor_order_precedes_release_preference(panel_layout):
    closer_debug = panel_layout.checkout / "host" / "panel" / ".build" / "debug" / "BobPanel"
    expected = _panel_file(closer_debug)
    _panel_file(panel_layout.release)
    assert panel_process.find_executable() == expected


def test_saved_selection_and_spawn_ignore_a_new_preferred_copy(panel_layout, monkeypatch):
    expected = _panel_file(panel_layout.bare)
    panel = PanelProcess()
    assert panel.executable_path == expected
    _panel_file(panel_layout.nested)
    assert PanelProcess().executable_path == str(panel_layout.nested)

    def no_resolve():
        pytest.fail("a saved-path read must not resolve again")

    monkeypatch.setattr(panel_process, "find_executable", no_resolve)
    captured = []
    proc = SimpleNamespace(pid=42, poll=lambda: None)
    monkeypatch.setattr(panel_process.subprocess, "Popen",
                        lambda argv, **kw: captured.append(argv) or proc)
    monkeypatch.setattr(panel_process.threading.Thread, "start", lambda self: None)
    assert panel._spawn()
    assert panel.alive()
    assert panel.executable_path == captured[-1][0] == expected
    assert captured[-1] == [expected, "--hidden"]
    proc.poll = lambda: 1
    assert not panel.alive()
    assert panel.executable_path == expected
    assert panel._spawn()
    assert captured == [[expected, "--hidden"], [expected, "--hidden"]]
    with pytest.raises(AttributeError):
        panel.executable_path = str(panel_layout.nested)


def test_unavailable_selection_stays_unavailable(panel_layout):
    panel = PanelProcess()
    assert panel.executable_path is None
    assert not panel.available
    _panel_file(panel_layout.nested)
    assert panel.executable_path is None
    assert not panel.available
    assert not panel._spawn()
    assert PanelProcess().executable_path == str(panel_layout.nested)
