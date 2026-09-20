import XCTest
import GravedecayKit

@MainActor
final class GraveInventoryTests: XCTestCase {
    func testSavedGravesAndSelectionSurviveRelaunchWithoutDiscovery() throws {
        let suite = "GravedecayInventoryTests." + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let first = GraveMenuModel(defaults: defaults, automaticallyRefresh: false)
        XCTAssertTrue(first.add("https://one.tail123.ts.net/grave/"))
        XCTAssertTrue(first.add("two.tail123.ts.net"))
        XCTAssertEqual(first.graves.count, 2)
        XCTAssertTrue(first.add("ONE.tail123.ts.net"))
        XCTAssertEqual(first.graves.count, 2)
        let selected = first.selectedID
        let restored = GraveMenuModel(defaults: defaults, automaticallyRefresh: false)
        XCTAssertEqual(restored.graves.count, 2); XCTAssertEqual(restored.selectedID, selected)
        XCTAssertEqual(restored.selected?.candidate.dns, "one.tail123.ts.net")
        XCTAssertTrue(restored.graves.allSatisfy { !$0.reachable })
        XCTAssertFalse(restored.add("https://someone@evil.example"))
        XCTAssertEqual(restored.selectedID, selected)
        restored.forget(try XCTUnwrap(restored.selected))
        let final = GraveMenuModel(defaults: defaults, automaticallyRefresh: false)
        XCTAssertEqual(final.graves.count, 1); XCTAssertEqual(final.selected?.candidate.dns, "two.tail123.ts.net")
    }
}
