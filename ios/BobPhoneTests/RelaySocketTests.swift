import XCTest
@testable import BobPhone

/// The socket lane's two pure rules: the one address this app builds, and
/// the reconnect ladder's shape.
@MainActor
final class RelaySocketTests: XCTestCase {
    private let channel = "0123456789abcdef0123456789abcdef"

    func testBuildsOnlyTheWSSFormWithChannelAndSide() {
        let url = RelaySocket.url(base: "wss://sock.example", channel: channel)
        XCTAssertEqual(url?.absoluteString,
                       "wss://sock.example/ws?ch=\(channel)&side=phone")
        let trailing = RelaySocket.url(base: " wss://sock.example/ \n", channel: channel)
        XCTAssertEqual(trailing?.absoluteString,
                       "wss://sock.example/ws?ch=\(channel)&side=phone")
        let nested = RelaySocket.url(base: "wss://sock.example/relay", channel: channel)
        XCTAssertEqual(nested?.absoluteString,
                       "wss://sock.example/relay/ws?ch=\(channel)&side=phone")
        let already = RelaySocket.url(base: "wss://sock.example/ws", channel: channel)
        XCTAssertEqual(already?.absoluteString,
                       "wss://sock.example/ws?ch=\(channel)&side=phone")
    }

    func testRefusesEveryOtherScheme() {
        XCTAssertNil(RelaySocket.url(base: "https://sock.example", channel: channel))
        XCTAssertNil(RelaySocket.url(base: "ws" + "://sock.example", channel: channel))
        XCTAssertNil(RelaySocket.url(base: "sock.example", channel: channel))
        XCTAssertNil(RelaySocket.url(base: "", channel: channel))
        XCTAssertNil(RelaySocket.url(base: "wss://", channel: channel))
    }

    func testTheLadderDoublesAndCapsAtMaxBackoff() {
        XCTAssertEqual(RelaySocket.ladder(steps: 7), [1, 2, 4, 8, 16, 30, 30])
        XCTAssertEqual(RelaySocket.nextBackoff(after: 30), RelaySocket.maxBackoff)
        XCTAssertEqual(RelaySocket.nextBackoff(after: 0), 2)
        XCTAssertEqual(RelaySocket.ladder(steps: 0), [])
        XCTAssertTrue(RelaySocket.ladder(steps: 20).allSatisfy { $0 <= RelaySocket.maxBackoff })
    }

    func testASocketWithABadBaseNeverOpensAndStaysOff() {
        let socket = RelaySocket(base: "https://sock.example", channel: channel)
        var states: [RelaySocket.State] = []
        socket.onState = { states.append($0) }
        socket.wanted = true
        XCTAssertEqual(socket.state, .off)
        XCTAssertFalse(socket.isOpen)
        XCTAssertEqual(states, [])
        socket.wanted = false
        XCTAssertEqual(socket.state, .off)
    }

    /// The relay's `peer:` words are read by the socket and never handed
    /// on: `peer:1` is the Mac arriving, `peer:0` leaving, anything else
    /// with the prefix is neither and is dropped; a fresh line has no peer.
    func testThePeerWordsAreParsedAndNeverHandedOn() {
        XCTAssertEqual(RelaySocket.peerWord("peer:1"), true)
        XCTAssertEqual(RelaySocket.peerWord("peer:0"), false)
        XCTAssertEqual(RelaySocket.peerWord(" peer:1\n"), true)
        XCTAssertNil(RelaySocket.peerWord("peer:2"))
        XCTAssertNil(RelaySocket.peerWord("peer:"))
        XCTAssertNil(RelaySocket.peerWord(""))
        XCTAssertNil(RelaySocket.peerWord("eyJhIjoxfQ=="))
        let socket = RelaySocket(base: "wss://sock.example", channel: channel)
        XCTAssertFalse(socket.peerPresent)
        XCTAssertFalse(socket.isOpen)
    }

    func testTheStateWordsAreTheProfilesWords() {
        XCTAssertEqual(RelaySocket.State.off.word, "")
        XCTAssertEqual(RelaySocket.State.connecting.word, "connecting")
        XCTAssertEqual(RelaySocket.State.open.word, "open")
    }
}
