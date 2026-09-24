# host/tests/test_phone_background_grace.py
"""The phone keeps its connection across a brief backgrounding.

`.background` no longer stops the poller: `PhoneClient.suspend()` stamps
`departedAt` and lands the counters, the loop polls nothing while departed
and stops itself past `BackgroundGrace.window` (300 s, under the 15-minute
background-refresh floor so a stale loop never shares the mailbox with a
`BGAppRefreshTask`'s own client), and the unlock gate keeps the poller and
asks for one check-in now (`wake()`) or starts fresh
(`BackgroundGrace.verdict`). The rule is Foundation-only and is run here
under `swiftc` — `test_phone_action_queue.py`'s seam — over the same table
`ios/BobPhoneTests/BackgroundGraceTests.swift` holds; the wiring is pinned
by source greps. Contract: `docs/phone-contract.md`, *A glance is not a
reconnect*.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
GRACE = PHONE / "BackgroundGrace.swift"
CLIENT = PHONE / "Client.swift"
APP = PHONE / "BobPhoneApp.swift"
REFRESH = PHONE / "BackgroundRefresh.swift"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
XCTEST = ROOT / "ios" / "BobPhoneTests" / "BackgroundGraceTests.swift"

HARNESS = r'''
import Foundation

struct Case: Decodable {
    var departed_seconds_ago: Double?
    var alive: Bool
}

let data = FileHandle.standardInput.readDataToEndOfFile()
let cases = try! JSONDecoder().decode([Case].self, from: data)
let now = Date(timeIntervalSince1970: 1_700_000_000)
print("WINDOW \(Int(BackgroundGrace.window))")
for c in cases {
    let departed = c.departed_seconds_ago.map { now.addingTimeInterval(-$0) }
    let verdict = BackgroundGrace.verdict(departedAt: departed, now: now, alive: c.alive)
    let expired = BackgroundGrace.expired(departedAt: departed, now: now)
    print("VERDICT \(verdict == .keep ? "keep" : "restart")")
    print("EXPIRED \(expired ? "true" : "false")")
}

// The pairing-record change handler's rule, over plain identities.
typealias Identity = BackgroundGrace.PairingIdentity
func identity(host: String = "10.0.0.2", hosts: [String] = ["10.0.0.2"],
              relayKey: String = "rk") -> Identity {
    Identity(token: "tok", host: host, hosts: hosts, port: 19875, deviceId: "dev",
             relayKey: relayKey, relayURL: "https://relay", homeKey: "hk",
             homeKeyCrossedInClear: false)
}
let same = identity()
let table: [(String, Identity?, Identity, Bool)] = [
    ("same-polling", same, identity(), true),
    ("same-not-polling", same, identity(), false),
    ("no-old", nil, identity(), true),
    ("promoted", same, identity(host: "10.0.0.9", hosts: ["10.0.0.9", "10.0.0.2"]), true),
    ("learned", same, identity(hosts: ["10.0.0.2", "mac.local"]), true),
    ("rekeyed", same, identity(relayKey: "other"), true),
]
for (name, old, new, polling) in table {
    let keeps = BackgroundGrace.keepsPoller(old: old, new: new, polling: polling)
    print("KEEPS \(name) \(keeps ? "true" : "false")")
}
'''


def _read(path: pathlib.Path) -> str:
    return path.read_text()


def _block(text: str, header: str) -> str:
    """The brace-balanced body following `header`."""
    i = text.index(header)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    raise AssertionError(header)


def _code(text: str) -> str:
    return "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("//"))


@pytest.fixture(scope="module")
def grace_bin(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.fail("swiftc is required on this macOS repository; it was not found")
    folder = tmp_path_factory.mktemp("phone-grace")
    main = folder / "main.swift"
    main.write_text(HARNESS)
    binary = folder / "phone-grace"
    built = subprocess.run(
        [swiftc, str(GRACE), str(main), "-o", str(binary)],
        capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    return binary


def _run(binary: pathlib.Path, cases: list[dict]) -> tuple[int, list[tuple[str, str]]]:
    proc = subprocess.run([str(binary)], input=json.dumps(cases),
                          capture_output=True, text=True, timeout=30,
                          check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    lines = proc.stdout.splitlines()
    assert lines[0].startswith("WINDOW ")
    window = int(lines[0].split()[1])
    rows = []
    rest = [line for line in lines[1:] if not line.startswith("KEEPS ")]
    for k in range(0, len(rest), 2):
        verdict = rest[k].split()[1]
        expired = rest[k + 1].split()[1]
        rows.append((verdict, expired))
    return window, rows


def _keeps(binary: pathlib.Path) -> dict[str, str]:
    """The identity rule's table, printed by the harness after the cases."""
    proc = subprocess.run([str(binary)], input="[]", capture_output=True,
                          text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return {line.split()[1]: line.split()[2]
            for line in proc.stdout.splitlines() if line.startswith("KEEPS ")}


# --- the rule, run ----------------------------------------------------------------


CASES = [
    # (departed seconds ago or None, alive, expected verdict, expected expired)
    (None, True, "keep", "false"),
    (30, True, "keep", "false"),
    (299, True, "keep", "false"),
    (300, True, "keep", "false"),
    (301, True, "restart", "true"),
    (301, False, "restart", "true"),
    (30, False, "restart", "false"),
    (None, False, "restart", "false"),
    (-60, True, "keep", "false"),
]


def test_verdict_keeps_inside_five_minutes_and_restarts_past_them(grace_bin):
    """The success criterion, run: within five minutes a live poller is
    kept (299 s, 300 s); past them (301 s) it restarts; nothing running
    restarts whatever the stamp; a clock that moved backwards keeps."""
    window, rows = _run(grace_bin, [
        {"departed_seconds_ago": ago, "alive": alive} for ago, alive, _, _ in CASES
    ])
    assert window == 300
    expected = [(v, e) for _, _, v, e in CASES]
    assert rows == expected, list(zip(CASES, rows))


def test_a_counters_only_record_change_keeps_the_running_poller(grace_bin):
    """The unlock gate's `load()` re-publishes the Keychain's record, whose
    counters `suspend()` moved at `.background`; `PairingRecord`'s
    synthesized `==` sees them, so `.onChange(of: pairing.record)` fires on
    every return. Only a running poller on the same pairing is kept —
    a changed address, list or key still restarts, as does a change with
    nothing running or with no record before it."""
    keeps = _keeps(grace_bin)
    assert keeps == {
        "same-polling": "true",
        "same-not-polling": "false",
        "no-old": "false",
        "promoted": "false",
        "learned": "false",
        "rekeyed": "false",
    }, keeps


def test_the_window_is_300_seconds_and_under_the_refresh_floor():
    grace = _read(GRACE)
    assert "static let window: TimeInterval = 300" in grace
    refresh = _read(REFRESH)
    intervals = re.search(r"static let intervals = \[(\d+)", refresh)
    assert intervals, "BackgroundRefresh.intervals"
    floor_seconds = int(intervals.group(1)) * 60
    assert 300 < floor_seconds
    # Foundation-only: `swiftc` runs it alone, and nothing here builds a URL,
    # reads a store or reaches the client.
    assert grace.count("import ") == 1 and "import Foundation" in grace
    code = _code(grace)
    for stranger in ("URLSession", "RelayChannel", "HomeChannel", "PhoneClient",
                     "UserDefaults", "Keychain"):
        assert stranger not in code, stranger


# --- the wiring ---------------------------------------------------------------------


def test_background_suspends_and_never_stops():
    app = _read(APP)
    background = app.split("case .background:")[1].split("@unknown default:")[0]
    assert "client.suspend()" in background
    assert "client.stop()" not in background
    assert "lock.lock()" in background
    assert "client.flushWidgetReload()" in background
    assert "BackgroundRefresh.schedule()" in background
    # The lock first, then the pause, then the widget's final publish.
    assert background.index("lock.lock()") < background.index("client.suspend()")
    assert background.index("client.suspend()") < background.index("client.flushWidgetReload()")


def test_the_unlock_gate_decides_keep_or_restart_in_one_place():
    app = _read(APP)
    gate = app[app.index(".onChange(of: lock.unlocked)"):]
    gate = gate[:gate.index(".onChange(of: pairing.record)")]
    assert "BackgroundGrace.verdict(" in gate
    assert "alive: client.isPolling" in gate
    assert "case .keep: client.wake()" in gate
    assert "case .restart: client.start(record: record)" in gate
    assert "if !client.isPolling" not in gate
    # `start` still precedes the registration nudge, and the nudge stays.
    assert gate.index("client.start(record: record)") < gate.index(
        "PushRegistrar.shared.ensure(record: record)")
    assert "client.stop()" not in _code(gate)


def test_the_pairing_record_handler_ignores_a_counters_only_change():
    """The handler after the gate binds `old` and asks the rule before it
    restarts: a `start` on the re-published record would undo `wake()` —
    cancel the check-in, drop the digest, show connecting."""
    app = _read(APP)
    handler = _block(app, ".onChange(of: pairing.record) { old, record in")
    assert "BackgroundGrace.keepsPoller(old: old?.identity," in handler
    assert "new: record.identity," in handler
    assert "polling: client.isPolling)" in handler
    # The restart is behind the guard, and the rest of the handler is as it
    # was: pair, then the registration nudge, and `stop()` on a cleared record.
    guarded = handler[handler.index("if !BackgroundGrace.keepsPoller("):]
    assert guarded.index("client.start(record: record)") < guarded.index("} else {")
    assert handler.count("client.start(record: record)") == 1
    assert handler.index("PhoneRouter.shared.pair(token: record.token)") < handler.index(
        "BackgroundGrace.keepsPoller(")
    assert "client.stop()" in handler
    assert "PushRegistrar.shared.ensure(record: record)" in handler
    # The bridge from the record is one computed property in the app file,
    # naming every field but the four counters.
    bridge = _block(app, "extension PairingRecord {")
    for field in ("token", "host", "hosts", "port", "deviceId", "relayKey",
                  "relayURL", "homeKey", "homeKeyCrossedInClear"):
        assert f"{field}: {field}" in bridge, field
    for counter in ("sendCtr", "recvCtr", "homeSendCtr", "homeRecvCtr"):
        assert counter not in bridge, counter
    # And the rule itself is Foundation-only, beside the verdict.
    grace = _read(GRACE)
    assert "struct PairingIdentity: Equatable" in grace
    assert "static func keepsPoller(old: PairingIdentity?, new: PairingIdentity," in grace


def test_suspend_stamps_and_flushes_and_tears_nothing_down():
    client = _read(CLIENT)
    assert "private(set) var departedAt: Date?" in client
    suspend = _block(client, "func suspend()")
    assert "departedAt = Date()" in suspend
    assert "flushCounters()" in suspend and "flushHomeCounters()" in suspend
    for torn in ("task?.cancel()", "channel = nil", "homeChannel = nil",
                 "record = nil", "polling?.cancel()", "heldStateDigest"):
        assert torn not in suspend, torn
    assert "guard task != nil else { return }" in suspend


def test_stop_clears_the_stamp():
    client = _read(CLIENT)
    stop = _block(client, "func stop()")
    assert "departedAt = nil" in stop
    assert "link = LinkAttempt()" in stop
    # `stop()` and `wake()` — the two sites that clear it.
    assert client.count("departedAt = nil") == 2


def test_the_loop_holds_while_departed_and_stops_itself_past_the_window():
    client = _read(CLIENT)
    start = _block(client, "func start(record: PairingRecord)")
    assert "BackgroundGrace.expired(departedAt: self?.departedAt, now: Date())" in start
    assert "if self?.departedAt != nil {" in start
    expired_at = start.index("BackgroundGrace.expired(")
    departed_at = start.index("if self?.departedAt != nil {")
    poll_at = start.index("await self?.poll(record)")
    assert expired_at < departed_at < poll_at
    # The departed tick makes no attempt: `noteAttempt()` is called once,
    # from the loop, after the poll — and nowhere else in the file. Its
    # sleep is `Task.sleep(for:)`, so the loop's one
    # `Task.sleep(nanoseconds: pause)` (`test_phone_reconnect_display.py`)
    # is still the cadence's only sleep.
    assert client.count("BackgroundGrace.expired(") == 1
    assert client.count("noteAttempt()") == 2  # the definition and the loop
    assert start.count("noteAttempt()") == 1
    assert start.index("await self?.poll(record)") < start.index("self?.noteAttempt()")
    departed_tick = start[departed_at:poll_at]
    assert "Task.sleep(for: .seconds(1))" in departed_tick
    assert "continue" in departed_tick
    assert "poll(" not in departed_tick and "noteAttempt" not in departed_tick


def test_wake_waits_out_a_cut_check_in_then_makes_one():
    client = _read(CLIENT)
    wake = _block(client, "func wake()")
    assert "guard departedAt != nil else { return }" in wake
    assert "departedAt = nil" in wake
    assert "await running.value" in wake
    # The slot, not the value: the loop's own continuation of `poll` clears
    # `polling` after the value's waiters may already have resumed, and a
    # `poll` made before that would join the finished task and make none.
    assert "while let running = self.polling {" in wake
    assert "await Task.yield()" in wake
    assert "if let running = self.polling" not in wake
    assert "probeHome: false" in wake
    assert "record.token == self.record?.token" in wake
    # Nothing in `suspend()`, `wake()` or the departed tick touches `phase`
    # or `link`: `test_phone_reconnect_display.py`'s counts stay.
    suspend = _block(client, "func suspend()")
    for body in (wake, suspend):
        assert "phase" not in body and "link." not in body
    assert client.count("phase = ") == 3


def test_a_late_check_in_lands_its_counters_again():
    client = _read(CLIENT)
    poll = _block(client, "private func poll(_ record: PairingRecord, probeHome: Bool = true) async")
    assert "if departedAt != nil {" in poll
    late = poll[poll.index("if departedAt != nil {"):]
    assert "flushCounters()" in late and "flushHomeCounters()" in late
    assert poll.index("pollsCompleted += 1") < poll.index("if departedAt != nil {")


def test_a_check_in_that_lands_while_departed_fires_no_onlive():
    """`suspend()` cancels nothing, so a check-in in flight at `.background`
    can land in the seconds before iOS suspends the process. It still
    applies the picture, the digest, the counters and `lastHeard` — but it
    must not fire `onLive`: the outbox drain and the `.sent` receipt replay
    it kicks call `RemoteAuth.authorize()` from away, whose `LAContext`
    cannot present from a backgrounded app, so a queued press would close
    `.stuck` "Face ID needed" with no prompt shown (`BackgroundRefresh`'s
    rule: never ask for Face ID from a process with nobody in front of it).
    Every `onLive?()` in the client is guarded on `departedAt == nil`; the
    drain waits for `wake()`'s check-in, made after the person unlocked."""
    client = _read(CLIENT)
    apply = _block(client, "private func applyState(")
    fired = client.count("onLive?()")
    guarded = client.count("if departedAt == nil { onLive?() }")
    assert fired >= 1
    assert fired == guarded, (fired, guarded)
    # Both sites are `applyState`'s — the keep branch and the applied one.
    assert apply.count("if departedAt == nil { onLive?() }") == 2
    assert "onLive?()" not in client.replace("if departedAt == nil { onLive?() }", "")


def test_both_swift_files_are_registered_in_the_project():
    pbx = _read(PBXPROJ)
    # Lines, `grep -c`'s count: a file reference, a build file, a group
    # child and a Sources entry for the app; the test target lists its
    # ids inline, so its file shows on the first two alone.
    assert sum("BackgroundGrace.swift" in line for line in pbx.splitlines()) == 4
    assert sum("BackgroundGraceTests.swift" in line for line in pbx.splitlines()) == 2
    # The test's file-reference id: its own line, the build file's `fileRef`
    # and the group's inline list; the build-file id: its own line and the
    # test target's Sources list.
    assert pbx.count("E5A5C0DE0000000000000503") == 3
    assert pbx.count("E5A5C0DE0000000000000504") == 2
    assert XCTEST.exists()
    xctest = _read(XCTEST)
    for case in ("testExactlyTheWindowKeeps", "testOneSecondPastTheWindowRestarts",
                 "testNothingRunningRestartsWhateverTheStamp",
                 "testAClockThatMovedBackwardsKeepsAndIsNotExpired"):
        assert f"func {case}()" in xctest, case
