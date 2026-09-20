import AppKit
import SwiftUI
import XCTest
import GravedecayKit

@MainActor
final class ManagementViewTests: XCTestCase {
    func testNativeManagementViewRendersSharedResources() async throws {
        let fixture = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "management", withExtension: "json"))
        ViewFixture.responses = try JSONSerialization.jsonObject(with: Data(contentsOf: fixture)) as! [String: Any]
        let model = ManagementModel(trackingURL: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString), makeClient: { host in
            let configuration = URLSessionConfiguration.ephemeral
            configuration.protocolClasses = [ViewFixture.self]
            return try ManagementAPI(host: host, configuration: configuration)
        })
        model.select("fixture.tail123.ts.net"); await model.refresh()
        XCTAssertEqual(model.system?.data?.cpu.usage_percent, 23)
        XCTAssertEqual(model.services?.data?.first?.id, "t3code")
        XCTAssertEqual(model.repositories?.data?.first?.changed_files, 3)
        XCTAssertNotNil(model.draft)
        let view = NSHostingView(rootView: MacManagementView(model: model, destination: model.host).graveRoot())
        view.frame = NSRect(x: 0, y: 0, width: 780, height: 2200)
        view.layoutSubtreeIfNeeded()
        let bitmap = try XCTUnwrap(view.bitmapImageRepForCachingDisplay(in: view.bounds))
        view.cacheDisplay(in: view.bounds, to: bitmap)
        let image = NSImage(size: view.bounds.size); image.addRepresentation(bitmap)
        let screenshot = XCTAttachment(image: image)
        screenshot.name = "Selected grave management — shared API fixture"
        screenshot.lifetime = .keepAlways; add(screenshot)

    }
}
private final class ViewFixture: URLProtocol, @unchecked Sendable {
    static var responses: [String: Any] = [:]
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let data = try! JSONSerialization.data(withJSONObject: Self.responses[request.url!.lastPathComponent]!)
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil,
            headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() { }
}
