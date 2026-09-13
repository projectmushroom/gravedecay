#if canImport(Darwin)
import Foundation
import XCTest
@testable import GravedecayKit

final class TtydWebSocketTests: XCTestCase {
    func testOriginUsesEndpointAuthorityWithoutCredentialsOrPath() throws {
        for (endpoint, origin) in [
            ("wss://box.example/term/ws?arg=claude", "https://box.example"),
            ("ws://127.0.0.1:3997/term/ws", "http://127.0.0.1:3997"),
            ("wss://user:password@box.example:8443/term/ws#fragment", "https://box.example:8443"),
            ("wss://box.example:443/term/ws", "https://box.example"),
            ("ws://box.example:80/ws", "http://box.example"),
            ("ws://[::1]:3997/ws", "http://[::1]:3997")
        ] {
            let url = try XCTUnwrap(URL(string: endpoint))
            let request = TtydWebSocket.request(for: url)
            XCTAssertEqual(request.url, url)
            XCTAssertEqual(request.value(forHTTPHeaderField: "Origin"), origin)
            XCTAssertEqual(request.value(forHTTPHeaderField: "Sec-WebSocket-Protocol"), "tty")
        }
    }

    func testTransportConnectsToOriginCheckingTtyd() throws {
        guard let endpoint = ProcessInfo.processInfo.environment["GRAVE_TTYD_TEST_URL"] else {
            throw XCTSkip("CI starts a real origin-checking ttyd for this test")
        }
        let received = expectation(description: "real ttyd launched the harmless fixture command")
        let connection = TtydWebSocket(url: try XCTUnwrap(URL(string: endpoint)))
        defer { connection.close() }
        let terminal = TtydSession(connection: connection)
        connection.onOpen = {
            terminal.start(token: "", columns: 80, rows: 24)
        }
        var sawOutput = false
        var output = Data()
        var frames: [String] = []
        connection.onClose = { error in
            if !sawOutput {
                XCTFail("ttyd transport closed before output: \(String(describing: error))")
                received.fulfill()
            }
        }
        connection.onFrame = { data in
            frames.append(String(decoding: data, as: UTF8.self))
            if data.first == UInt8(ascii: "0") {
                output.append(data.dropFirst())
                if !sawOutput && String(decoding: output, as: UTF8.self).contains("grave-origin-ready") {
                    sawOutput = true
                    received.fulfill()
                }
            }
        }
        connection.connect()
        wait(for: [received], timeout: 15)
        XCTAssertTrue(sawOutput, "received frames: \(frames)")
    }
}
#endif
