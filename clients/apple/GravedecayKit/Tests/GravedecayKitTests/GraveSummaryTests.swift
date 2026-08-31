import XCTest
@testable import GravedecayKit

final class GraveSummaryTests: XCTestCase {
    func testTailscaleStateKeepsUnavailableDistinctFromMissing() {
        XCTAssertEqual(GraveDiscovery.tailscaleState(executableFound: false, statusData: nil), .missing)
        XCTAssertEqual(GraveDiscovery.tailscaleState(executableFound: true, statusData: nil), .unavailable)
        XCTAssertEqual(GraveDiscovery.tailscaleState(executableFound: true, statusData: Data(#"{"BackendState":"NeedsLogin"}"#.utf8)), .loggedOut)
        XCTAssertEqual(GraveDiscovery.tailscaleState(executableFound: true, statusData: Data(#"{"BackendState":"Stopped"}"#.utf8)), .loggedOut)
        XCTAssertEqual(GraveDiscovery.tailscaleState(executableFound: true, statusData: Data(#"{"BackendState":"NeedsMachineAuth"}"#.utf8)), .loggedOut)
        XCTAssertEqual(GraveDiscovery.tailscaleState(executableFound: true, statusData: Data(#"{"BackendState":"Running"}"#.utf8)), .running)
        XCTAssertEqual(GraveDiscovery.tailscaleState(executableFound: true, statusData: Data(#"{}"#.utf8)), .unavailable)
        XCTAssertEqual(GraveDiscovery.tailscaleState(executableFound: true, statusData: Data(#"{"BackendState":"Starting"}"#.utf8)), .unavailable)
    }

    func testCandidatesFilterAndDeduplicate() {
        let input = #"{"Self":{"Online":true,"DNSName":"One.Tail.ts.net.","ID":"1"},"Peer":{"a":{"Online":true,"DNSName":"two.tail.ts.net","StableID":"2"},"b":{"Online":true,"DNSName":"bad_thing","ID":"3"},"c":{"Online":true,"DNSName":"other.tail.ts.net","ID":"1"}}}"#.data(using: .utf8)!
        XCTAssertEqual(GraveDiscovery.candidates(statusData: input), [GraveCandidate(id: "1", dns: "one.tail.ts.net", name: "one.tail.ts.net"), GraveCandidate(id: "2", dns: "two.tail.ts.net", name: "two.tail.ts.net")])
    }
    func testSelectedGraveFallsBackToFirstReachableCandidate() {
        let graves = [GraveCandidate(id: "first", dns: "first.tail.ts.net", name: "first"), GraveCandidate(id: "second", dns: "second.tail.ts.net", name: "second")]
        XCTAssertEqual(GraveDiscovery.selectedID(previousID: nil, candidates: graves), "first")
        XCTAssertEqual(GraveDiscovery.selectedID(previousID: "second", candidates: graves), "second")
        XCTAssertEqual(GraveDiscovery.selectedID(previousID: "missing", candidates: graves), "first")
        XCTAssertNil(GraveDiscovery.selectedID(previousID: "second", candidates: []))
    }
    func testSummaryValidationAndFormatting() {
        let data = #"{"product":"gravedecay","api_version":1,"observed_at":"2026-08-23T12:00:00Z","node":{"host":"grave","platform":"macos","mode":"developer","uptime_s":90061},"resources":{"cpu_pct":4.2,"memory_pct":31.1,"disk_pct":42,"cpu_temp_c":null,"gpu_temp_c":54},"activity":{"sessions_live":1,"sessions_frozen":2},"health":{"services_failed":1,"containers_problem":2},"links":{"dashboard":"/grave/","t3":"/","terminal":"/term/","network":"/net/"}}"#.data(using: .utf8)!
        XCTAssertEqual(GraveSummary.decode(data)?.problems, 3)
        XCTAssertEqual(GravePresentation.percent(4.2), "4%")
        XCTAssertEqual(GravePresentation.temperature(54), "54°C")
        XCTAssertEqual(GravePresentation.uptime(90061), "1d 1h")
        XCTAssertNil(GraveSummary.decode(Data("{}".utf8)))
    }
    func testSafeSameHostLinks() {
        XCTAssertEqual(GravePresentation.link(host: "grave.tail.ts.net", path: "/grave/")?.host, "grave.tail.ts.net")
        XCTAssertEqual(GravePresentation.link(host: "GRAVE.tail.ts.net.", path: "/grave/")?.host, "grave.tail.ts.net")
        XCTAssertNil(GravePresentation.link(host: "gråve.tail.ts.net", path: "/grave/"))
        XCTAssertNil(GravePresentation.link(host: "grave.tail.ts.net", path: "//elsewhere"))
        XCTAssertNil(GravePresentation.link(host: "grave.tail.ts.net", path: "/bad\\path"))
    }

    func testCapabilitiesRequirePublishedSafeTerminal() throws {
        let summary = try XCTUnwrap(GraveSummary.decode(Data(#"{"product":"gravedecay","api_version":1,"node":{"host":"grave","platform":"macos","mode":"companion"},"resources":{},"activity":{"sessions_live":0,"sessions_frozen":0},"health":{"services_failed":0,"containers_problem":0},"links":{"dashboard":"/grave/","terminal":"/elsewhere","network":"/net/"}}"#.utf8)))
        XCTAssertNil(summary.terminal)
        XCTAssertNil(GravePresentation.safePath("/grave/../term"))
        XCTAssertNil(GraveDiscovery.dnsName("-bad.ts.net"))
        XCTAssertNil(GraveDiscovery.dnsName("grave.ts.n3t"))
        XCTAssertEqual(GraveDiscovery.dnsName(" Grave-1.Tail.ts.net. "), "grave-1.tail.ts.net")
    }

    func testTerminalTokenResponseClassification() {
        XCTAssertEqual(TerminalTokenResponse.classify(data: Data(#"{"token":"abc"}"#.utf8), statusCode: 200), .token("abc"))
        XCTAssertEqual(TerminalTokenResponse.classify(data: Data(#"{"token":""}"#.utf8), statusCode: 200), .token(""))
        XCTAssertEqual(TerminalTokenResponse.classify(data: Data(#"{"token":null}"#.utf8), statusCode: 200), .token(""))
        XCTAssertEqual(TerminalTokenResponse.classify(data: Data(#"{}"#.utf8), statusCode: 200), .invalidResponse)
        XCTAssertEqual(TerminalTokenResponse.classify(data: Data(#"{"token":1}"#.utf8), statusCode: 200), .invalidResponse)
        XCTAssertEqual(TerminalTokenResponse.classify(data: Data(), statusCode: 404), .http(404))
    }

    func testConditionsAndClampedFormatting() {
        let summary = GraveSummary.decode(#"{"product":"gravedecay","api_version":1,"node":{"host":"grave","platform":"macos","mode":"developer","uptime_s":-1},"resources":{},"activity":{"sessions_live":1,"sessions_frozen":1},"health":{"services_failed":-2,"containers_problem":3},"links":{}}"#.data(using: .utf8)!)!
        XCTAssertEqual(summary.problems, 3)
        XCTAssertEqual(GravePresentation.condition(summary: summary, reachable: false), .unreachable)
        XCTAssertEqual(GravePresentation.condition(summary: summary, reachable: true), .warning)
        let active = GraveSummary.decode(#"{"product":"gravedecay","api_version":1,"node":{"host":"grave","platform":"macos","mode":"developer","uptime_s":0},"resources":{},"activity":{"sessions_live":1,"sessions_frozen":1},"health":{"services_failed":0,"containers_problem":0},"links":{}}"#.data(using: .utf8)!)!
        XCTAssertEqual(GravePresentation.condition(summary: active, reachable: true), .active)
        let frozen = GraveSummary.decode(#"{"product":"gravedecay","api_version":1,"node":{"host":"grave","platform":"macos","mode":"developer","uptime_s":0},"resources":{},"activity":{"sessions_live":0,"sessions_frozen":1},"health":{"services_failed":0,"containers_problem":0},"links":{}}"#.data(using: .utf8)!)!
        XCTAssertEqual(GravePresentation.condition(summary: frozen, reachable: true), .frozen)
        let healthy = GraveSummary.decode(#"{"product":"gravedecay","api_version":1,"node":{"host":"grave","platform":"macos","mode":"developer","uptime_s":0},"resources":{},"activity":{"sessions_live":0,"sessions_frozen":0},"health":{"services_failed":0,"containers_problem":0},"links":{}}"#.data(using: .utf8)!)!
        XCTAssertEqual(GravePresentation.condition(summary: healthy, reachable: true), .healthy)
        XCTAssertEqual(GravePresentation.condition(summary: nil, reachable: true), .unreachable)
        XCTAssertEqual(GravePresentation.uptime(-1), "0m")
        XCTAssertTrue(GravePresentation.uptime(1e308).hasSuffix("d 3h"))
        XCTAssertEqual(GravePresentation.uptime(.infinity), "—")
        XCTAssertEqual(GravePresentation.age(nil), "—")
        XCTAssertEqual(GravePresentation.age(Date.now.addingTimeInterval(-7_200)), "2 hours ago")
        XCTAssertEqual(GravePresentation.age(Date.now.addingTimeInterval(-172_800)), "2 days ago")
    }

    func testProblemCountSaturates() {
        let summary = GraveSummary.decode(#"{"product":"gravedecay","api_version":1,"node":{"host":"grave","platform":"macos","mode":"developer","uptime_s":0},"resources":{},"activity":{"sessions_live":0,"sessions_frozen":0},"health":{"services_failed":9223372036854775807,"containers_problem":9223372036854775807},"links":{}}"#.data(using: .utf8)!)!
        XCTAssertEqual(summary.problems, Int.max)
    }
}
