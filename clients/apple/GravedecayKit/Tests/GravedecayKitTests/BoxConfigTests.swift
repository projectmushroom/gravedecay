import XCTest
@testable import GravedecayKit

final class BoxConfigTests: XCTestCase {
    func testNormalizesPastedInput() {
        for input in [
            "box.tail1234.ts.net",
            "  https://box.tail1234.ts.net/  ",
            "https://box.tail1234.ts.net/grave/",
            "BOX.tail1234.ts.net",
            "wss://box.tail1234.ts.net:443/term/ws",
        ] {
            XCTAssertEqual(BoxConfig(input: input)?.host, "box.tail1234.ts.net", "input: \(input)")
        }
    }

    func testRejectsGarbage() {
        XCTAssertNil(BoxConfig(input: ""))
        XCTAssertNil(BoxConfig(input: "   "))
        XCTAssertNil(BoxConfig(input: "https://"))
        XCTAssertNil(BoxConfig(input: "not a hostname"))
    }

    func testURLLayoutMatchesTailscaleServe() throws {
        let box = try XCTUnwrap(BoxConfig(input: "box.ts.net"))
        XCTAssertEqual(box.t3URL.absoluteString, "https://box.ts.net/")
        XCTAssertEqual(box.dashboardURL.absoluteString, "https://box.ts.net/grave/")
        XCTAssertEqual(box.terminalURL.absoluteString, "https://box.ts.net/term")
        XCTAssertEqual(box.terminalTokenURL.absoluteString, "https://box.ts.net/term/token")
    }

    func testTerminalWebSocketURL() throws {
        let box = try XCTUnwrap(BoxConfig(input: "box.ts.net"))
        XCTAssertEqual(box.terminalWebSocketURL().absoluteString, "wss://box.ts.net/term/ws")
        XCTAssertEqual(box.terminalWebSocketURL(arg: "agents").absoluteString,
                       "wss://box.ts.net/term/ws?arg=agents")
    }
}
