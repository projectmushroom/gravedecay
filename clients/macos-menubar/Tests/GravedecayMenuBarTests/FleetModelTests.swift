import XCTest
@testable import GravedecayMenuBar
import GravedecayKit

@MainActor
final class FleetModelTests: XCTestCase {
    private let status = Data(#"{"BackendState":"Running","Self":{"Online":true,"DNSName":"self.tail.ts.net","ID":"self"},"Peer":{"one":{"Online":true,"DNSName":"one.tail.ts.net","StableID":"one"},"offline":{"Online":false,"DNSName":"offline.tail.ts.net","ID":"offline"}}}"#.utf8)
    private let good = GraveSummary.decode(Data(#"{"product":"gravedecay","api_version":1,"node":{"host":"One","platform":"macos","mode":"developer"},"resources":{},"activity":{"sessions_live":0,"sessions_frozen":0},"health":{"services_failed":0,"containers_problem":0},"links":{"dashboard":"/grave/"}}"#.utf8))!

    func testKeepsOnlyReachableValidatedSummaries() async {
        let status = status, good = good
        let model = FleetModel(start: false, statusLoader: { (true, status) }, summaryLoader: { candidate in candidate.id == "one" ? good : nil })
        await model.scan()
        XCTAssertEqual(model.boxes.map(\.id), ["one"])
        XCTAssertEqual(model.state, .ready)
        XCTAssertFalse(model.needsAttention)
        XCTAssertEqual(model.menuState.title, "Gravedecay · All clear")
    }

    func testEmptyFleetIsNeutral() {
        let model = FleetModel(start: false, statusLoader: { (false, nil) }, summaryLoader: { _ in nil })
        XCTAssertEqual(model.menuState.title, "Gravedecay · No reachable graves")
        XCTAssertEqual(model.menuState.symbol, "circle.dashed")
    }

    func testDashboardUsesValidatedSameHostPath() async {
        let status = status, good = good
        let model = FleetModel(start: false, statusLoader: { (true, status) }, summaryLoader: { _ in good })
        await model.scan()
        XCTAssertEqual(model.dashboardURL(for: try! XCTUnwrap(model.boxes.first))?.absoluteString, "https://one.tail.ts.net/grave/")
    }
}
