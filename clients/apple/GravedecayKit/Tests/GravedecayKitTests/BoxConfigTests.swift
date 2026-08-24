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
        let netstat = "Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll\nlo0 16384 <Link#1> 00:00 1 0 2 3 0 4 0\ngif0* 1280 <Link#2> 00:00 0 0 0 0 0 0 0\nen5 1500 <Link#8> aa:bb 11 0 100 12 0 200 0\n"
        let interfaces = NativeParsers.interfaceBytes(netstat)
        XCTAssertEqual(interfaces.count, 1); XCTAssertEqual(interfaces.first?.0, "en5"); XCTAssertEqual(interfaces.first?.1, 100); XCTAssertEqual(interfaces.first?.2, 200)
    }

    func testGitHubRunParserUsesConclusionAndRejectsUnsafeURLs() {
        let success = #"[{"databaseId":7,"displayTitle":"Build","workflowName":"CI","status":"completed","conclusion":"success","url":"https://github.com/a/b/actions/runs/7"}]"#
        XCTAssertEqual(NativeParsers.githubRun(Data(success.utf8))?.state, "success")
        let running = #"[{"databaseId":8,"workflowName":"CI","status":"in_progress","conclusion":"","url":"https://github.com/a/b/actions/runs/8"}]"#
        XCTAssertEqual(NativeParsers.githubRun(Data(running.utf8))?.state, "in_progress")
        let bad = #"[{"databaseId":9,"workflowName":"CI","status":"completed","url":"https://github.com.evil/a"}]"#
        XCTAssertNil(NativeParsers.githubRun(Data(bad.utf8)))
        let long = "[{\"databaseId\":10,\"displayTitle\":\"" + String(repeating: "x", count: 241) + "\",\"status\":\"completed\",\"url\":\"https://github.com/a/b/actions/runs/10\"}]"
        XCTAssertNil(NativeParsers.githubRun(Data(long.utf8)))
    }
}
