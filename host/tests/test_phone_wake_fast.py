"""The away wake, made fast (25 Sep 2026).

Four pieces, pinned off the Swift source (the phone is another language)
and off the daemon:

1. A request made while the socket is still coming up waits a moment for
   it (`RelaySocket.readyGrace`) instead of going the mailbox way.
2. The socket relay's `peer:1` wakes the Mac's mailbox connector out of its
   idle sleep (`relay.note_phone_arrived`; `test_relay_client.py` and
   `test_relay_ws.py` carry the behaviour).
3. The check-in a person waits on tries the home address for
   `wakeProbe`, not the walk's eight seconds, while the socket comes up.
4. The socket starts coming up while Face ID runs (`prewarm`).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"


def _read(name: str) -> str:
    return (PHONE / name).read_text(encoding="utf-8")


def test_a_request_waits_briefly_for_a_socket_coming_up():
    transport = _read("RelayTransport.swift")
    body = transport.split("func request(kind: String")[1].split("\n    func ")[0]
    wait = body.index("socket.comingUp")
    assert "await socket.ready(within: RelaySocket.readyGrace)" in body
    assert wait < body.index("socket.isOpen, socket.peerPresent"), (
        "the wait must come before the socket-or-mailbox choice")
    socket = _read("RelaySocket.swift")
    assert "static let readyGrace: TimeInterval = 1.5" in socket
    assert "static let peerGrace: TimeInterval = 0.4" in socket
    coming = socket.split("var comingUp: Bool {")[1].split("\n    }")[0]
    assert "guard wanted else { return false }" in coming
    # The reconnect ladder's sleep is `.connecting` with no task: nothing is
    # coming, so nothing waits (bug audit, 25 Sep 2026).
    assert "if state == .connecting { return task != nil }" in coming
    # A `peer:0` read on arrival ends the grace.
    assert "!heardPeer" in coming
    assert "self.heardPeer = true" in socket
    teardown = socket.split("private func teardown() {")[1].split("\n    }")[0]
    assert "heardPeer = false" in teardown


def test_a_home_answer_closes_the_line_and_a_missed_quick_probe_walks_home():
    client = _read("Client.swift")
    body = client.split("private func pollOnce(")[1].split("\n    private func ")[0]
    assert "if via == .lan { channel?.socket?.wanted = false }" in body
    relay_rung = body.split("await pollViaRelay(channel, record: record)")[-1]
    # Gated on `quick` alone: `wake()` polls with `probeHome: false`.
    assert "if quick {" in relay_rung and "if quick, probeHome" not in body
    assert "candidates: candidates," in relay_rung
    # At home the line comes up only once the quick probe missed, just
    # before the relay rung it serves.
    last_rung = body.rsplit("await pollViaRelay(channel, record: record)", 1)[0]
    assert last_rung.rstrip().endswith(
        "if quick, departedAt == nil { channel.socket?.wanted = true }")


def test_a_failed_unlock_closes_the_prewarmed_line():
    client = _read("Client.swift")
    abandon = client.split("func abandonPrewarm() {")[1].split("\n    }")[0]
    assert "guard departedAt != nil else { return }" in abandon
    assert "channel?.socket?.wanted = false" in abandon
    app = _read("BobPhoneApp.swift")
    assert "if !lock.unlocked { client.abandonPrewarm() }" in app


def test_the_waited_on_check_in_gives_home_one_second():
    client = _read("Client.swift")
    assert "static let wakeProbe: TimeInterval = 1\n" in client
    poll = client.split("private func pollOnce(")[1].split("\n    private func ")[0]
    assert poll.index("let waking = wakeCheckIn") < poll.index("if knowsItIsAway")
    assert "wakeCheckIn = false" in poll
    assert "let quick = waking && channel != nil" in poll
    assert "firstTimeout: quick ? Self.wakeProbe : nil" in poll
    assert "candidates: quick ? [record.host] : candidates" in poll
    direct = client.split("private func pollDirect(")[1].split("\n    private func ")[0]
    assert "(firstTimeout ?? 8)" in direct


def test_the_socket_comes_up_during_face_id_and_sends_nothing():
    client = _read("Client.swift")
    prewarm = client.split("func prewarm()")[1].split("\n    }")[0]
    assert "channel?.socket?.wanted = true" in prewarm
    assert "request(" not in prewarm and "poll(" not in prewarm
    app = _read("BobPhoneApp.swift")
    active = app.split("case .active:")[1].split("case .inactive:")[0]
    assert active.index("client.prewarm()") < active.index("lock.unlock()")


def test_the_mac_wakes_its_mailbox_on_peer_arrival():
    daemon = ROOT / "host" / "dark_army_daemon"
    ws = (daemon / "relay_ws.py").read_text(encoding="utf-8")
    assert 'if message == "peer:1":' in ws
    assert "relay.note_phone_arrived(device_id)" in ws
    client = (daemon / "relay_client.py").read_text(encoding="utf-8")
    assert "PEER_ARM_SECONDS = 90.0" in client
    assert "await asyncio.wait_for(arrival.wait(), gap)" in client


def test_the_app_open_unlock_covers_every_away_write():
    """Face ID once, when the app opens: a successful unlock grants the
    remote-write gate until the app locks again, so no action asks a second
    time (25 Sep 2026)."""
    auth = _read("RemoteAuth.swift")
    authorize = auth.split("func authorize(")[1].split("\n    }")[0]
    assert authorize.index("if sessionGranted { return true }") < authorize.index(
        "LAContext()"), "the opening's grant must be read before any prompt"
    grant = auth.split("func grantForSession() {")[1].split("\n    }")[0]
    assert "sessionGranted = true" in grant
    reset = auth.split("func reset() {")[1].split("\n    }")[0]
    assert "sessionGranted = false" in reset
    app = _read("BobPhoneApp.swift")
    unlock = app.split("func unlock() async {")[1].split("\n    }\n")[0]
    assert "if ok { RemoteAuth.shared.grantForSession() }" in unlock
    lock = app.split("func lock() {")[1].split("\n    }")[0]
    assert "RemoteAuth.shared.reset()" in lock
    # An unlock resolving after a lock is stale: it grants nothing.
    assert "epoch += 1" in lock
    assert unlock.index("let started = epoch") < unlock.index("evaluatePolicy(")
    assert "answered && epoch == started" in unlock
    assert unlock.index("answered && epoch == started") < unlock.index(
        "RemoteAuth.shared.grantForSession()")
