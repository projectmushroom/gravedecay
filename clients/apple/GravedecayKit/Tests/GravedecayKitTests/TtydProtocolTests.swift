import XCTest
@testable import GravedecayKit

final class TtydProtocolTests: XCTestCase {
    func json(_ data: Data) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    func testHelloCarriesTokenAndSize() throws {
        let hello = TtydProtocol.hello(token: "s3cret", columns: 120, rows: 40)
        let obj = try json(hello)
        XCTAssertEqual(obj["AuthToken"] as? String, "s3cret")
        XCTAssertEqual(obj["columns"] as? Int, 120)
        XCTAssertEqual(obj["rows"] as? Int, 40)
    }

    func testBinaryInputFrame() {
        let frame = TtydProtocol.inputFrame([0x1b, 0x5b, 0x41]) // up arrow
        XCTAssertEqual(Array(frame), [UInt8(ascii: "0"), 0x1b, 0x5b, 0x41])
    }

    func testResizeFrame() throws {
        let frame = TtydProtocol.resizeFrame(columns: 80, rows: 24)
        XCTAssertEqual(frame.first, UInt8(ascii: "1"))
        let obj = try json(Data(frame.dropFirst()))
        XCTAssertEqual(obj["columns"] as? Int, 80)
        XCTAssertEqual(obj["rows"] as? Int, 24)
    }

    func testPauseResumeFrames() {
        XCTAssertEqual(Array(TtydProtocol.pauseFrame), [UInt8(ascii: "2")])
        XCTAssertEqual(Array(TtydProtocol.resumeFrame), [UInt8(ascii: "3")])
    }

    func testParseOutput() {
        var frame = Data([UInt8(ascii: "0")])
        frame.append(contentsOf: Array("hi".utf8))
        XCTAssertEqual(TtydProtocol.parse(frame), Data("hi".utf8))
    }

    func testParseIgnoresTitleAndPreferences() {
        XCTAssertNil(TtydProtocol.parse(Data([UInt8(ascii: "1"), 0x41])))
        XCTAssertNil(TtydProtocol.parse(Data([UInt8(ascii: "2"), UInt8(ascii: "{"), UInt8(ascii: "}")])))
    }

    func testParseRejectsEmptyAndUnknown() {
        XCTAssertNil(TtydProtocol.parse(Data()))
        XCTAssertNil(TtydProtocol.parse(Data([UInt8(ascii: "9"), 0x41])))
    }

    /// parse must not choke on Data slices with a non-zero base index.
    func testParseHandlesSlicedData() {
        let padded = Data([0xff, UInt8(ascii: "0"), 0x41])
        let sliced = Data(padded.dropFirst())
        XCTAssertEqual(TtydProtocol.parse(sliced), Data([0x41]))
    }
}
