import XCTest
@testable import BobPanel

final class CodexReviewTests: XCTestCase {
    private func payload(_ count: Int = 5) throws -> String {
        let findings: [[String: Any]] = (0..<count).map { i in
            ["title": "[P1] Finding \(i)", "body": "Explanation \(i)\nSecond line",
             "confidence_score": 0.98,
             "code_location": ["absolute_file_path": "/fixture/source\(i).swift",
                               "line_range": ["start": i + 1, "end": i + 3]]]
        }
        return String(decoding: try JSONSerialization.data(withJSONObject: [
            "findings": findings, "overall_correctness": "patch is incorrect",
            "overall_explanation": "Five separate issues.", "future": true], options: .prettyPrinted), as: UTF8.self)
    }
    func testFiveFindingsHaveIndependentPresentation() throws {
        let review = try XCTUnwrap(CodexReview.decode(payload()))
        XCTAssertEqual(review.findings.count, 5)
        for (i, finding) in review.findings.enumerated() {
            XCTAssertEqual(finding.title, "[P1] Finding \(i)")
            XCTAssertEqual(finding.explanation, "Explanation \(i)\nSecond line")
            XCTAssertEqual(finding.path, "/fixture/source\(i).swift")
            XCTAssertEqual(finding.start, i + 1)
            XCTAssertEqual(finding.end, i + 3)
        }
        XCTAssertEqual(review.conclusion, "patch is incorrect")
        XCTAssertEqual(review.explanation, "Five separate issues.")
    }
    func testEmptyFindingsRetainConclusion() throws {
        let review = try XCTUnwrap(CodexReview.decode(payload(0)))
        XCTAssertTrue(review.findings.isEmpty)
        XCTAssertEqual(review.explanation, "Five separate issues.")
    }
    func testMissingAndMalformedOptionalLocationsDoNotLoseWords() {
        let source = #"{"findings":[{"title":"One","body":"Why","code_location":42},{"title":"Two","body":"Because"}],"overall_correctness":"correct","overall_explanation":"Finished"}"#
        let review = CodexReview.decode(source)
        XCTAssertEqual(review?.findings.map(\.explanation), ["Why", "Because"])
        XCTAssertEqual(review?.findings.map(\.path), ["", ""])
    }
    func testRawPreservesNewlinesAndRejectsPartialNonReviewOrTruncated() throws {
        for source in ["{\n broken\n}", "{\n\"findings\": []\n}", "[\n1\n]"] {
            XCTAssertEqual(CodexReview.presentation(source), .raw(source))
        }
        let source = try payload()
        XCTAssertEqual(CodexReview.presentation(source, truncated: true), .raw(source))
        XCTAssertEqual(CodexReview.presentation("Ordinary\nprose"), .prose("Ordinary\nprose"))
    }
    func testOversizeIsBoundedRaw() {
        let source = "{\n" + String(repeating: "x", count: 100_000)
        XCTAssertNil(CodexReview.decode(source))
        guard case .raw(let raw) = CodexReview.presentation(source) else { return XCTFail() }
        XCTAssertTrue(raw.hasPrefix("{\n"))
        XCTAssertLessThan(raw.utf8.count, 65600)
        XCTAssertTrue(raw.hasSuffix("output truncated"))
    }
    func testReportsDecodeTolerantlyAndDoNotBorrowRootIdentity() throws {
        let decoder = JSONDecoder()
        let old = try decoder.decode(Agent.self, from: Data(#"{"session_id":"root"}"#.utf8))
        XCTAssertTrue(old.reviewReports.isEmpty)
        let source = #"{"session_id":"root","review_reports":[{"session_id":"root","text":"first"},{"session_id":"root","text":"second","truncated":true},{}],"review_reports_omitted":2}"#
        let agent = try decoder.decode(Agent.self, from: Data(source.utf8))
        XCTAssertEqual(agent.reviewReports.count, 3)
        XCTAssertEqual(agent.reviewReports[0].text, "first")
        XCTAssertEqual(agent.reviewReports[1].text, "second")
        XCTAssertTrue(agent.reviewReports[1].truncated)
        XCTAssertEqual(agent.reviewReportsOmitted, 2)
        XCTAssertFalse(agent.channel)
        XCTAssertFalse(agent.canType)
        XCTAssertTrue(agent.replyOptions.isEmpty)
    }
}

extension CodexReviewTests {
    @MainActor
    func testHideActionTargetsSelectedRootNotReportIdentity() async throws {
        let received = expectation(description: "hide request")
        XCTAssertTrue(URLProtocol.registerClass(CodexHideSpy.self))
        defer {
            URLProtocol.unregisterClass(CodexHideSpy.self)
            CodexHideSpy.observe = nil
        }
        CodexHideSpy.observe = { body in
            XCTAssertEqual(body["action"] as? String, "hide_session")
            XCTAssertEqual(body["session_id"] as? String, "codex:parent")
            received.fulfill()
        }
        let row = try JSONDecoder().decode(Agent.self, from: Data(#"{"session_id":"codex:parent","can_hide":true,"review_reports":[{"session_id":"codex:child","text":"review"}]}"#.utf8))
        let actions = RowActions()
        let client = DaemonClient()
        actions.dismiss(row, client: client)
        await fulfillment(of: [received], timeout: 3)
    }
}

private final class CodexHideSpy: URLProtocol {
    static var observe: (([String: Any]) -> Void)?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var data = request.httpBody ?? Data()
        if data.isEmpty, let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var bytes = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&bytes, maxLength: bytes.count)
                if count <= 0 { break }
                data.append(contentsOf: bytes.prefix(count))
            }
        }
        Self.observe?((try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:])
        let response = HTTPURLResponse(url: request.url!, statusCode: 200,
                                       httpVersion: "HTTP/1.1", headerFields: [:])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data("{}".utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
