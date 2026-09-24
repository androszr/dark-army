import XCTest

/// The pairing screen's address checker, compiled into this target through a
/// symlink to `ios/BobPhone/HostAddress.swift` — one source of truth, so there
/// is nothing to drift. The phone app has no test target by explicit decision;
/// these are the rules' only automated home.
final class HostAddressTests: XCTestCase {
    private func cleaned(_ host: String, _ port: Int = 19875) -> HostAddress.Cleaned? {
        if case .ok(let value) = HostAddress.check(host: host, typedPort: port) { return value }
        return nil
    }

    private func refusal(_ host: String, _ port: Int = 19875) -> String? {
        if case .refused(let message) = HostAddress.check(host: host, typedPort: port) { return message }
        return nil
    }

    func testBareAddressIsAccepted() {
        XCTAssertEqual(cleaned("192.168.1.23"), .init(host: "192.168.1.23", port: 19875))
    }

    func testWhitespaceIsTrimmed() {
        XCTAssertEqual(cleaned("  192.168.1.23\n"), .init(host: "192.168.1.23", port: 19875))
    }

    func testSchemeIsStripped() {
        XCTAssertEqual(cleaned("http://192.168.1.23"), .init(host: "192.168.1.23", port: 19875))
    }

    func testSchemeIsStrippedCaseInsensitively() {
        XCTAssertEqual(cleaned("HTTPS://mac.local"), .init(host: "mac.local", port: 19875))
    }

    func testTrailingSlashIsStripped() {
        XCTAssertEqual(cleaned("192.168.1.23/"), .init(host: "192.168.1.23", port: 19875))
    }

    func testWholePasteKeepsHostAndEmbeddedPort() {
        XCTAssertEqual(cleaned("http://192.168.1.23:19875/"),
                       .init(host: "192.168.1.23", port: 19875))
    }

    func testEmbeddedPortBeatsTypedPort() {
        XCTAssertEqual(cleaned("192.168.1.23:19999", 19875),
                       .init(host: "192.168.1.23", port: 19999))
    }

    func testSchemeOnlyIsRefused() {
        XCTAssertNotNil(refusal("http://"))
    }

    func testSpaceIsRefusedAndSaidSo() {
        let message = refusal("my mac")
        XCTAssertNotNil(message)
        XCTAssertTrue(message?.contains("space") == true, message ?? "")
    }

    func testPathIsRefused() {
        let message = refusal("mac.local/api/pair")
        XCTAssertNotNil(message)
        XCTAssertTrue(message?.contains("web address") == true, message ?? "")
    }

    func testNonNumericPortIsRefused() {
        let message = refusal("192.168.1.23:abc")
        XCTAssertNotNil(message)
        XCTAssertTrue(message?.contains("port number") == true, message ?? "")
    }

    func testZeroPortIsRefused() {
        XCTAssertEqual(refusal("192.168.1.23", 0),
                       "The port needs to be a number between 1 and 65535.")
    }

    func testPortAboveRangeIsRefused() {
        XCTAssertEqual(refusal("192.168.1.23", 70000),
                       "The port needs to be a number between 1 and 65535.")
    }

    func testEmbeddedPortAboveRangeIsRefused() {
        XCTAssertEqual(refusal("http://192.168.1.23:99999/"),
                       "The port needs to be a number between 1 and 65535.")
    }

    func testBracketedIPv6IsRefused() {
        let message = refusal("[::1]")
        XCTAssertNotNil(message)
        XCTAssertTrue(message?.contains("IPv6") == true, message ?? "")
    }

    func testBareIPv6IsRefused() {
        let message = refusal("fe80::1")
        XCTAssertNotNil(message)
        XCTAssertTrue(message?.contains("IPv6") == true, message ?? "")
    }

    func testBadCharactersAreRefused() {
        XCTAssertNotNil(refusal("héllo"))
    }

    func testUnderscoreHostIsAccepted() {
        XCTAssertEqual(cleaned("my_mac.local"), .init(host: "my_mac.local", port: 19875))
    }

    func testEmptyHostIsRefused() {
        XCTAssertNotNil(refusal(""))
    }

    func testEveryRefusalIsACompleteSentence() {
        let junk = ["my mac", "mac.local/api/pair", "192.168.1.23:abc", "[::1]",
                    "fe80::1", "héllo", "http://", ""]
        for text in junk {
            guard let message = refusal(text) else {
                XCTFail("expected a refusal for \(text)")
                continue
            }
            XCTAssertFalse(message.isEmpty)
            XCTAssertTrue(message.hasSuffix("."), message)
        }
    }
}
