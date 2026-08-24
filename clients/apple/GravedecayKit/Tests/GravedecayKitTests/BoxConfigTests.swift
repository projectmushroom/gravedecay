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

    func testGitPorcelainCount() {
        XCTAssertEqual(WorkStatus.changedFileCount(""), 0)
        XCTAssertEqual(WorkStatus.changedFileCount(" M one\n?? two\n"), 2)
    }

    func testGitHubRemoteAllowlist() {
        XCTAssertEqual(GitHubRemote.repository("git@github.com:projectmushroom/gravedecay.git"), "projectmushroom/gravedecay")
        XCTAssertEqual(GitHubRemote.url("https://github.com/projectmushroom/gravedecay")?.host, "github.com")
        XCTAssertNil(GitHubRemote.repository("https://github.com.evil.example/a/b"))
    }

    func testNativeParsersAreBoundedAndSafe() {
        XCTAssertEqual(NativeParsers.cpuUsage("CPU usage: 12.3% user, 4.5% sys, 83.2% idle"), "12.3% user, 4.5% sys")
        XCTAssertEqual(NativeParsers.githubRows(Data(#"[{"number":3,"title":"safe","state":"OPEN","url":"https://github.com/a/b/pull/3"},{"number":4,"title":"bad","state":"OPEN","url":"https://evil.example"}]"#.utf8)).count, 1)
        XCTAssertEqual(NativeParsers.interfaceBytes("en0 1500 x x x x 10 0 20 0\n").first?.1, 10)
    }
}
