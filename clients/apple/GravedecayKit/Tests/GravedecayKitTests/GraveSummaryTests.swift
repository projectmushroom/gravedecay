import XCTest
@testable import GravedecayKit

final class GraveSummaryTests: XCTestCase {
    func testCandidatesFilterAndDeduplicate() {
        let input = #"{"Self":{"Online":true,"DNSName":"One.Tail.ts.net.","ID":"1"},"Peer":{"a":{"Online":true,"DNSName":"two.tail.ts.net","StableID":"2"},"b":{"Online":true,"DNSName":"bad_thing","ID":"3"},"c":{"Online":true,"DNSName":"other.tail.ts.net","ID":"1"}}}"#.data(using: .utf8)!
        XCTAssertEqual(GraveDiscovery.candidates(statusData: input), [GraveCandidate(id: "1", dns: "one.tail.ts.net", name: "one.tail.ts.net"), GraveCandidate(id: "2", dns: "two.tail.ts.net", name: "two.tail.ts.net")])
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
        XCTAssertNil(GravePresentation.link(host: "grave.tail.ts.net", path: "//elsewhere"))
        XCTAssertNil(GravePresentation.link(host: "grave.tail.ts.net", path: "/bad\\path"))
    }
}
