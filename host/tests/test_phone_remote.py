# host/tests/test_phone_remote.py
"""The phone's half of the away path, pinned from the Mac's side.

Source pins over the Swift files (`test_phone_top_bar.py`'s pattern —
`ios/` has no test target by explicit decision, and this suite is the one
that actually runs). What is pinned is the **cross-platform contract**: the
HKDF info strings and AAD prefix byte-for-byte against `relay.py`, only
`https` relay URLs, the Keychain accessibility class, the Face ID gate on
remote writes, and the rule that the device header never travels to a relay.
"""

from __future__ import annotations

from pathlib import Path

from dark_army_daemon import relay

ROOT = Path(__file__).resolve().parents[2]

PHONE = ROOT / "ios" / "BobPhone"
TRANSPORT = PHONE / "RelayTransport.swift"
REMOTE_AUTH = PHONE / "RemoteAuth.swift"
PAIRING = PHONE / "Pairing.swift"
HOME_TRANSPORT = PHONE / "HomeTransport.swift"
CLIENT = PHONE / "Client.swift"
ACTIONS = PHONE / "Actions.swift"
APP = PHONE / "BobPhoneApp.swift"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


# --- the cross-platform crypto contract ---------------------------------------


def test_transport_carries_the_same_hkdf_info_strings():
    text = _read(TRANSPORT)
    assert f'Data("{relay.INFO_CHANNEL_ID.decode()}".utf8)' in text
    assert f'Data("{relay.INFO_P2M.decode()}".utf8)' in text
    assert f'Data("{relay.INFO_M2P.decode()}".utf8)' in text


def test_transport_carries_the_same_aad_prefix():
    assert f'Data("{relay.AAD_PREFIX.decode()}".utf8)' in _read(TRANSPORT)


def test_transport_matches_the_skew_and_size_bounds():
    text = _read(TRANSPORT)
    assert f"skewSeconds: TimeInterval = {relay.RELAY_SKEW_SECONDS}" in text
    assert f"frameMaxBytes = {relay.RELAY_FRAME_MAX_BYTES:_}" in text


def test_transport_uses_empty_salt_and_combined_boxes():
    text = _read(TRANSPORT)
    assert "salt: Data()" in text
    assert "ChaChaPoly" in text
    assert ".combined" in text


def test_transport_speaks_raw_deflate():
    # COMPRESSION_ZLIB is raw DEFLATE, matching relay.py's wbits=-15.
    assert "COMPRESSION_ZLIB" in _read(TRANSPORT)
    assert relay._DEFLATE_WBITS == -15


# --- https only, and the device header never travels --------------------------


def test_transport_builds_only_https_urls():
    text = _read(TRANSPORT)
    assert text.count("http://") == 0
    assert 'scheme == "https"' in text


def test_transport_never_carries_the_device_header():
    text = _read(TRANSPORT)
    assert "X-Bob-Device" not in text
    assert "deviceHeader" not in text


def test_client_relay_rung_goes_through_the_channel_only():
    """`Client.swift` has two sealed channels and no header of its own: the
    home legs ride `HomeChannel`, the away rung `RelayChannel`, and the old
    device header is gone from every line."""
    text = _read(CLIENT)
    assert "X-Bob-Device" not in text
    assert "deviceHeader" not in text
    assert "HomeChannel" in text
    assert "RelayChannel" in text
    assert "pollViaRelay" in text


def test_home_transport_carries_the_home_namespace():
    """The home path's four strings, beside the relay's four, and the home
    transport seals in that namespace alone."""
    text = _read(TRANSPORT)
    for literal in ("bob-home v1 channel-id", "bob-home v1 p2m",
                    "bob-home v1 m2p", "bob-home v1|"):
        assert f'Data("{literal}".utf8)' in text, literal
    for literal in (relay.HOME.info_channel_id, relay.HOME.info_p2m,
                    relay.HOME.info_m2p, relay.HOME.aad_prefix):
        assert f'Data("{literal.decode()}".utf8)' in text
    home = _read(HOME_TRANSPORT)
    assert "ns: .home" in home
    assert "ns: .relay" not in home


def test_home_transport_is_the_only_home_url_builder():
    """`Client.swift` builds no `http://` URL of its own any more; the home
    transport builds through `PhoneActions.homeURL` / `uploadURL`, and the
    relay transport still builds only `https`."""
    client = _read(CLIENT)
    for line in client.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("///"):
            continue
        assert "http://" not in line, line
    home = _read(HOME_TRANSPORT)
    assert "PhoneActions.homeURL" in home
    assert "PhoneActions.uploadURL" in home
    assert "http://" not in home
    assert "X-Bob-Device" not in home
    assert "PhoneActions.channelHeader" in home
    assert "PhoneActions.frameHeader" in home
    assert "RelayTransport.sealBlob" in home
    assert "Task.detached" in home


# --- the key store and the write gate -----------------------------------------


def test_pairing_still_pins_the_keychain_class():
    """This-device-only, behind the passcode — and, since the tile's
    background check-in, readable after first unlock rather than only while
    unlocked (`test_phone_background_refresh` pins the move)."""
    text = _read(PAIRING)
    assert text.count("kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly") >= 1
    assert "ThisDeviceOnly" in text and "kSecAttrAccessibleAlways" not in text


def test_pairing_record_decodes_relay_fields_tolerantly():
    text = _read(PAIRING)
    assert "relayKey" in text
    assert "decodeIfPresent(String.self, forKey: .relayKey)" in text
    assert "decodeIfPresent(Int.self, forKey: .sendCtr)" in text
    # The socket relay's address decodes the same way: a record from before
    # the socket lane simply has no socket.
    assert "decodeIfPresent(String.self, forKey: .relayWSURL)" in text
    assert 'relayWSURL: obj["relay_ws_url"] as? String ?? ""' in text
    # The home key and its counters decode the same tolerant way: a record
    # from before sealed home access reads as keyless, never as no record.
    assert "decodeIfPresent(String.self, forKey: .homeKey)" in text
    assert "decodeIfPresent(Int.self, forKey: .homeSendCtr)" in text
    assert "decodeIfPresent(Int.self, forKey: .homeRecvCtr)" in text


def test_remote_auth_gates_with_the_device_owner_policy():
    text = _read(REMOTE_AUTH)
    assert "LAContext" in text
    assert "evaluatePolicy" in text
    assert ".deviceOwnerAuthentication" in text


def test_client_asks_for_the_face_before_a_remote_write():
    """Both write paths through the relay — actions and prepare — pass the
    RemoteAuth gate before anything is sealed."""
    text = _read(CLIENT)
    assert text.count("RemoteAuth.shared.authorize()") >= 2


def test_remote_grace_dies_with_the_pairing_and_the_lock():
    """`RemoteAuth.reset()` is actually called — from the un-pair path and
    from the lock — so the 5-minute write grace cannot outlive either."""
    assert _read(APP).count("RemoteAuth.shared.reset()") >= 2


def test_counters_are_persisted_and_resumed():
    """The counter chain has no dead links: the app assigns both closures,
    the store writes the Keychain, and the channel is rebuilt from the
    Keychain's own counters — unwired, every relaunch rewinds to 0 and
    reopens the replay window the plan's risk row 3 claims closed."""
    app = _read(APP)
    assert "client.onCounters" in app
    assert "pairing.persistCounters(token:" in app
    assert "client.storedCounters" in app
    pairing = _read(PAIRING)
    assert "func persistCounters(token: String, send: Int, recv: Int)" in pairing
    assert "func storedCounters() -> (send: Int, recv: Int)?" in pairing
    client = _read(CLIENT)
    assert "storedCounters?()" in client
    # And the home channel's own pair, wired the same way.
    assert "client.onHomeCounters" in app
    assert "pairing.persistHomeCounters(token:" in app
    assert "client.storedHomeCounters" in app
    assert "func persistHomeCounters(token: String, send: Int, recv: Int)" in pairing
    assert "func storedHomeCounters() -> (send: Int, recv: Int)?" in pairing
    assert "storedHomeCounters?()" in client


def test_stale_counters_cannot_poison_a_new_pairing():
    """A dead pairing's counters must never wind a fresh record's forward.

    The failure mode: an old channel's late answer fires `onCounters` after
    `stop()`, staging counters from the dead pairing; on re-pair the flush
    lands them in the new record, `buildChannel`'s `max(record, stored)`
    starts the fresh channel at the old `recvCtr`, and every one of the
    Mac's new answers (ctr 1, 2, …) is refused as a replay — actions run
    while reported failed. Three braces, any one of which is enough:
    `stop()` severs the dying channel's callback; `flushCounters` drops a
    staged token that is not the current record's; and `persistCounters`
    refuses counters stamped with another pairing's token — the guard that
    holds even if a future call site forgets to sever."""
    client = _read(CLIENT)
    assert "channel?.onCounters = nil" in client
    assert "homeChannel?.onCounters = nil" in client
    assert "pending.token == record?.token" in client
    pairing = _read(PAIRING)
    assert "guard token == current.token else { return }" in pairing


def test_channel_requests_are_serialised():
    """`RelayChannel.request` chains through one in-flight task at a time.
    Two concurrent waiters both RPOP the same `to-phone` list, and a pop is
    a removal — the waiter that takes the wrong reply has destroyed it.

    The queue is **keyed by mailbox and static**, because the invariant is
    one waiter per process: `BackgroundRefresh` builds a second
    `PhoneClient`, and so a second `RelayChannel` over the same mailbox, and
    an instance-scoped queue lets those two pop against each other."""
    text = _read(TRANSPORT)
    assert "private static var inFlight: [String: Task<Answer, Never>] = [:]" in text
    assert "let mailbox = RelayTransport.channelId(key: key)" in text
    assert "let previous = Self.inFlight[mailbox]" in text
    assert "_ = await previous?.value" in text


def test_an_abandoned_request_stops_popping_the_mailbox():
    """The queued task is unstructured, so cancellation is forwarded by hand.
    Without it the background refresh's cut-off leg keeps a poller alive that
    iOS freezes with the process and wakes on the next launch — popping the
    foreground poll's answers out of the mailbox."""
    text = _read(TRANSPORT)
    assert "withTaskCancellationHandler" in text
    assert "onCancel: {\n            task.cancel()\n        }" in text
    # Both legs: before the envelope is sealed and posted, and again at the
    # top of every poll of the mailbox.
    assert text.count("if Task.isCancelled { return Answer(failure: "
                      "Trouble.cutShort) }") == 2
    assert 'static let cutShort = "The check-in was cut short."' in text


# --- the refusal words --------------------------------------------------------


def test_lease_refusal_prefix_matches_the_daemon():
    text = _read(ACTIONS)
    assert 'leaseRefusalPrefix = "away access has lapsed"' in text
    assert relay.LEASE_REFUSAL.startswith("away access has lapsed")


def test_the_four_away_failures_are_told_apart():
    """One sentence for four failures is what made the last outage
    undiagnosable from the phone. Each of these names a different thing to
    do about it: check your signal, wait out a rate limit, wait out the
    mailbox's storage, or wait for the Mac to wake."""
    text = _read(TRANSPORT)
    assert '"No connection to the relay — check your internet."' in text
    assert '"The relay refused the request (rate-limited)."' in text
    assert '"The relay\'s storage is unavailable."' in text
    assert '"The relay is reachable but the Mac has not answered — "' in text
    assert '"The relay answered unexpectedly (code \\(code))."' in text
    # The old catch-all is gone from every path.
    assert "Could not reach the relay." not in text


def test_the_deadline_covers_the_macs_idle_pickup():
    """The connector backs off to a 30-second gap once a channel has been
    quiet, so the default poll deadline must outlast one idle wake-up or a
    working link reports itself broken on the first request after a lull.
    The bound is the connector's own `IDLE_GAP_MAX`."""
    from dark_army_daemon import relay_client
    text = _read(TRANSPORT)
    assert "timeout: TimeInterval = 45" in text
    assert 45 > relay_client.IDLE_GAP_MAX


def test_the_macs_own_refusals_are_never_reworded():
    """A sealed `err` frame carries the daemon's words — the lease refusal
    among them — and is shown verbatim. The new wording is for the transport
    failures only, which the Mac never saw."""
    text = _read(TRANSPORT)
    assert 'failure: payload["error"] as? String' in text
    assert '?? "The Mac refused the request.")' in text
    assert '"An envelope failed verification."' in text


def test_photos_stay_on_home_wifi():
    assert 'photosNeedHome = "Photos need home Wi-Fi."' in _read(ACTIONS)
    assert "PhoneActions.photosNeedHome" in _read(CLIENT)


# --- the write prompt does not lock the app -----------------------------------


def _authorize_body(text: str) -> str:
    return text.split("func authorize(")[1].split("\n    }")[0]


def test_the_write_prompt_does_not_lock_the_app():
    """The Face ID / passcode sheet in front of an away write drives the
    scene `.inactive` exactly as a consent alert does. Locking there
    unmounted the card or agent the press was made on and then asked a
    second time on `.active`. `RemoteAuth` raises `prompting` before the
    sheet, and the app ORs it into the same carve-out the two consent
    alerts already have."""
    body = _authorize_body(_read(REMOTE_AUTH))
    assert body.index("prompting = true") < body.index("evaluatePolicy(")
    assert "defer { prompting = false }" in body
    app = _read(APP)
    inactive = app.split("case .inactive:")[1].split("case .background:")[0]
    assert "RemoteAuth.shared.prompting" in inactive
    assert inactive.index("RemoteAuth.shared.prompting") < inactive.index(
        "lock.lock()")
    # And the two consent carve-outs are still there beside it.
    assert "DictationEngine.shared.requestingConsent" in inactive
    assert "PushRegistrar.shared.requestingConsent" in inactive


def test_the_prompt_flag_is_raised_only_after_the_availability_guard():
    """A phone with no passcode returns early from `canEvaluatePolicy`,
    which skips the `defer`. Raising the flag above that guard would leave
    it up for ever and disable the lock on every later `.inactive`."""
    body = _authorize_body(_read(REMOTE_AUTH))
    assert body.index("canEvaluatePolicy") < body.index("prompting = true")


def test_the_prompt_flag_lowers_on_every_exit():
    """One raise, covered by one `defer` — so a refusal and a thrown
    `LAError.userCancel` (the person tapped Cancel) lower it exactly as
    success does."""
    body = _authorize_body(_read(REMOTE_AUTH))
    assert body.count("prompting = true") == 1
    raised_at = body.index("prompting = true")
    deferred_at = body.index("defer { prompting = false }")
    assert raised_at < deferred_at < body.index("evaluatePolicy(")


def test_backgrounding_still_locks_with_no_carve_outs():
    """Putting the phone down or switching apps locks immediately. The
    carve-outs are `.inactive`'s alone, which is what bounds the
    app-switcher snapshot residual they all share."""
    app = _read(APP)
    background = app.split("case .background:")[1].split("@unknown default:")[0]
    assert "lock.lock()" in background
    for name in ("prompting", "requestingConsent", "consenting",
                 "scannerUp", "lock.busy"):
        assert name not in background


# --- the project compiles them ------------------------------------------------


def test_new_files_are_in_the_xcode_project():
    text = _read(PBXPROJ)
    # Twice each: the PBXBuildFile entry and the Sources build phase line.
    assert text.count("RelayTransport.swift in Sources") == 2
    assert text.count("RemoteAuth.swift in Sources") == 2
    assert text.count("HomeTransport.swift in Sources") == 2
    assert text.count("LinkTiming.swift in Sources") == 2
    assert text.count("RelaySocket.swift in Sources") == 2
    # The test target's Sources phase is the uncommented one-line list, so
    # the PBXBuildFile comment is the one occurrence; membership itself is
    # `test_phone_xcode_membership.py`'s.
    assert text.count("LinkTimingTests.swift in Sources") == 1
    assert text.count("RelaySocketTests.swift in Sources") == 1


# --- the link timing ----------------------------------------------------------


LINK_TIMING = PHONE / "LinkTiming.swift"
MODELS = PHONE / "Models.swift"


def test_both_channels_read_the_macs_timing_tolerantly():
    """The Mac's legs ride the reply envelope as `timing`; an older Mac
    sends none, and both channels read the key with the one tolerant
    cast — absent stays `[:]`, never a throw and never a blank answer."""
    for path in (TRANSPORT, HOME_TRANSPORT):
        text = _read(path)
        assert 'payload["timing"] as? [String: Double] ?? [:]' in text, path.name
        assert "var onTiming: ((LinkTimingSample) -> Void)?" in text, path.name
    assert "var macTiming: [String: Double] = [:]" in _read(TRANSPORT)
    assert "var legs = LinkLegs()" in _read(TRANSPORT)


def test_the_timing_opt_in_is_behind_the_marker():
    """`accessLogReport` asks for the Mac's rollups only where the Mac
    states `link_timing_supported`: an older Mac would 400 the unknown
    query key and the whole screen would go blank."""
    client = _read(CLIENT)
    body = client.split("func accessLogReport(")[1].split("\n    func ")[0]
    assert 'if snapshot.board.linkTimingSupported { query += "&timing=1" }' in body
    assert body.count("timing=1") == 1
    models = _read(MODELS)
    assert models.count("link_timing_supported") == 1
    assert "linkTimingSupported = c.value(.linkTimingSupported, false)" in models
    assert "hops = c.value(.hops, [:])" in models


def test_the_timing_store_carries_no_secret_and_builds_no_url():
    text = _read(LINK_TIMING)
    for forbidden in ("URL(string", "URLSession", "AnthropicKeyStore",
                      "PairingStore", "Keychain", "http", "X-Bob"):
        assert forbidden not in text, forbidden
    # Written protected, with the attribute belt beside it.
    assert "options: [.atomic, .completeFileProtection]" in text
    assert "FileProtectionType.complete" in text
    # The file is stamped with the pairing-generation token (the held
    # picture's stamp), never a key, an address or a credential.
    assert "var pairingToken: String" in text
    for forbidden in ("homeKey", "relayKey", "home_key", "relay_key", "host:"):
        assert forbidden not in text, forbidden
    # The encode and the write run off the main actor: a nonisolated async
    # function inside the scheduling task, so cancellation is seen there.
    assert "private nonisolated static func write(" in text
    assert 'static let fileName = "link-timing.json"' in text
    client = _read(CLIENT)
    assert "linkTiming.forget()" in client
    # A re-pair without an un-pair rebases the ring beside the held picture.
    assert "linkTiming.adopt(record.token)" in client
    assert "func adopt(_ token: String)" in text
    # The write is floored and re-checks cancellation on both sides of the
    # file, so `forget` never has the file put back under it.
    assert "minWriteInterval" in text
    assert text.count("Task.isCancelled") >= 3
    # The widget's client and the lock-screen client wire no timing.
    background = client.split("func backgroundRefresh(")[1].split("\n    func ")[0]
    assert "onTiming" not in background
    lock = client.split("func lockScreenWrite(")[1].split("\n    func ")[0]
    assert "onTiming" not in lock


# --- the socket lane ------------------------------------------------------------


RELAY_SOCKET = PHONE / "RelaySocket.swift"


def test_the_socket_builds_only_wss_addresses_and_holds_no_key():
    """`RelaySocket.swift` is the one phone file that builds a socket
    address, and it builds `wss://` alone; the transport keeps building
    only `https` mailbox URLs. Nothing in the socket file seals, opens or
    holds a key — the channel does all three on both sides of it."""
    text = _read(RELAY_SOCKET)
    assert text.count('scheme == "wss"') == 1
    assert "ws://" not in text
    assert "http://" not in text
    assert "URLSessionWebSocketTask" in text
    assert 'URLQueryItem(name: "side", value: "phone")' in text
    for forbidden in ("RelayTransport.seal", "RelayTransport.open", "ChaChaPoly",
                      "relayKey", "homeKey", "PairingStore", "Keychain"):
        assert forbidden not in text, forbidden
    assert "http://" not in _read(TRANSPORT)
    assert 'scheme == "https"' in _read(TRANSPORT)
    assert "URLSessionWebSocketTask" not in _read(TRANSPORT)


def test_the_channel_tries_the_socket_first_and_falls_through_to_the_mailbox():
    text = _read(TRANSPORT)
    assert "var socket: RelaySocket?" in text
    assert "var onPush: (([String: Any]) -> Void)?" in text
    assert "var onSocketState: ((RelaySocket.State) -> Void)?" in text
    assert "static let socketDeadline: TimeInterval = 10" in text
    assert "func tookSocketFrame(_ text: String)" in text
    body = text.split("func request(")[1].split("\n    private func send(")[0]
    assert "socket.isOpen" in body
    assert "sendViaSocket(kind: kind, body: body" in body
    assert body.index("sendViaSocket") < body.index("self.send(kind: kind")
    # The mailbox loop is untouched: its own lines still hold.
    assert "_ = await previous?.value" in body
    assert "private static var inFlight: [String: Task<Answer, Never>] = [:]" in text
    assert 'route: LinkTimingRoute.socket' in text


def test_link_timing_names_the_socket_route():
    text = _read(LINK_TIMING)
    assert 'static let socket = "ws"' in text
    assert "[LinkTimingRoute.away, LinkTimingRoute.socket, LinkTimingRoute.home]" in text
    assert 'if sample.kind == "push"' in text


def test_the_client_opens_the_socket_on_screen_and_away_only():
    client = _read(CLIENT)
    start = client.split("func start(record: PairingRecord)")[1].split("\n    func ")[0]
    assert "buildChannel(record, socket: true)" in start
    assert "channel?.onPush" in start and "channel?.onSocketState" in start
    background = client.split("func backgroundRefresh(")[1].split("\n    func ")[0]
    lock = client.split("func lockScreenWrite(")[1].split("\n    func ")[0]
    for body in (background, lock):
        assert "socket: true" not in body
        assert "onPush" not in body
    assert "channel?.socket?.wanted = via == .relay && departedAt == nil" in client
    suspend = client.split("func suspend()")[1].split("\n    func ")[0]
    assert "channel?.socket?.wanted = false" in suspend
    wake = client.split("func wake()")[1].split("\n    private func ")[0]
    assert "if via == .relay { channel?.socket?.wanted = true }" in wake
    push = client.split("private func tookPush(")[1].split("\n    private func ")[0]
    assert "guard departedAt == nil, knowsItIsAway" in push
    assert "applyState(body, record: record, route: .relay)" in push
    assert 'kind: "push"' in push and "LinkTimingRoute.socket" in push
    assert "@Published var socket: String" in client
    assert 'row(label: "socket", value: away.socket)' in _read(PHONE / "ProfileView.swift")
