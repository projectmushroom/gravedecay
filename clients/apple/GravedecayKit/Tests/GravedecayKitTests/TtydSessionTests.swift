import XCTest
@testable import GravedecayKit

/// Records frames like a mock websocket and lets tests act as the terminal
/// (holding on to `done` callbacks to simulate an async render queue).
private final class Harness: TtydConnection, TtydSessionDelegate {
    var sentFrames: [Data] = []
    var outputs: [Data] = []
    var pendingDone: [() -> Void] = []
    var titles: [String] = []
    var prefs: [Data] = []

    func sendFrame(_ data: Data) { sentFrames.append(data) }

    func ttydSession(_ session: TtydSession, output: Data, done: @escaping () -> Void) {
        outputs.append(output)
        pendingDone.append(done)
    }

    func ttydSession(_ session: TtydSession, setTitle title: String) { titles.append(title) }
    func ttydSession(_ session: TtydSession, preferences: Data) { prefs.append(preferences) }

    var sentCommands: [UInt8] { sentFrames.compactMap(\.first) }

    func outputFrame(byteCount: Int) -> Data {
        var frame = Data([UInt8(ascii: "0")])
        frame.append(Data(repeating: 0x41, count: byteCount))
        return frame
    }
}

final class TtydSessionTests: XCTestCase {
    private var harness: Harness!
    private var session: TtydSession!

    override func setUp() {
        harness = Harness()
        session = TtydSession(connection: harness, delegate: harness)
    }

    func testStartSendsHello() throws {
        session.start(token: "tok", columns: 100, rows: 30)
        let obj = try XCTUnwrap(
            JSONSerialization.jsonObject(with: XCTUnwrap(harness.sentFrames.first)) as? [String: Any])
        XCTAssertEqual(obj["AuthToken"] as? String, "tok")
    }

    func testOutputRoutedToDelegate() {
        session.receive(harness.outputFrame(byteCount: 5))
        XCTAssertEqual(harness.outputs, [Data(repeating: 0x41, count: 5)])
        XCTAssertEqual(harness.sentFrames, [], "small writes must not trigger flow control")
    }

    func testTitleAndPreferencesRouted() {
        var title = Data([UInt8(ascii: "1")])
        title.append(contentsOf: Array("t".utf8))
        session.receive(title)
        XCTAssertEqual(harness.titles, ["t"])

        session.receive(Data([UInt8(ascii: "2"), UInt8(ascii: "{"), UInt8(ascii: "}")]))
        XCTAssertEqual(harness.prefs, [Data("{}".utf8)])
    }

    // Flow control semantics from app.js: a chunk that pushes the running
    // byte count over `limit` becomes "pending" (and resets the count);
    // more than `highWater` pending chunks → PAUSE; once the terminal has
    // drained below `lowWater` → RESUME.
    func testFlowControlPausesAndResumes() {
        let flow = TtydSession.FlowLimits(limit: 10, highWater: 3, lowWater: 2)
        session = TtydSession(connection: harness, delegate: harness, flow: flow)

        // Each 11-byte chunk crosses the limit → each becomes pending.
        for _ in 0..<4 {
            session.receive(harness.outputFrame(byteCount: 11))
        }
        // pending is now 4 > highWater(3): exactly one PAUSE sent.
        XCTAssertEqual(harness.sentCommands, [TtydProtocol.pause])

        // Terminal consumes two chunks: pending 4→2, 2 < lowWater? no (2 < 2 false).
        harness.pendingDone.removeFirst()()
        harness.pendingDone.removeFirst()()
        XCTAssertEqual(harness.sentCommands, [TtydProtocol.pause])

        // Third consumption: pending 1 < lowWater(2) → RESUME.
        harness.pendingDone.removeFirst()()
        XCTAssertEqual(harness.sentCommands, [TtydProtocol.pause, TtydProtocol.resume])

        // Draining the rest must not resume twice.
        harness.pendingDone.removeFirst()()
        XCTAssertEqual(harness.sentCommands, [TtydProtocol.pause, TtydProtocol.resume])
    }

    func testFlowControlDoesNotPauseTwice() {
        let flow = TtydSession.FlowLimits(limit: 10, highWater: 1, lowWater: 1)
        session = TtydSession(connection: harness, delegate: harness, flow: flow)

        for _ in 0..<5 {
            session.receive(harness.outputFrame(byteCount: 11))
        }
        XCTAssertEqual(harness.sentCommands.filter { $0 == TtydProtocol.pause }.count, 1)
    }

    func testSmallWritesAccumulateTowardsLimit() {
        let flow = TtydSession.FlowLimits(limit: 10, highWater: 0, lowWater: 1)
        session = TtydSession(connection: harness, delegate: harness, flow: flow)

        session.receive(harness.outputFrame(byteCount: 6))
        XCTAssertEqual(harness.sentFrames, [])
        // 6 + 6 = 12 > 10 → this chunk is tracked, pending(1) > highWater(0) → PAUSE.
        session.receive(harness.outputFrame(byteCount: 6))
        XCTAssertEqual(harness.sentCommands, [TtydProtocol.pause])
        // The first chunk's done is a no-op; the tracked chunk's is the last one.
        harness.pendingDone.removeLast()()
        XCTAssertEqual(harness.sentCommands, [TtydProtocol.pause, TtydProtocol.resume])
    }

    func testInputAndResizePassThrough() {
        session.send(text: "x")
        session.resize(columns: 81, rows: 25)
        XCTAssertEqual(harness.sentCommands, [TtydProtocol.input, TtydProtocol.resize])
    }
}
