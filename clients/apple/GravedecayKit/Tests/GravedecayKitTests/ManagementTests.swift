import XCTest
@testable import GravedecayKit
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

private enum Fixture {
    static let document = try! JSONSerialization.jsonObject(with: Data(contentsOf: Bundle.module.url(forResource: "management", withExtension: "json", subdirectory: "Fixtures")!)) as! [String: Any]
    static func data(_ name: String) -> Data { try! JSONSerialization.data(withJSONObject: document[name]!) }
    static func reply(_ request: URLRequest) -> Stub {
        let name = request.url!.lastPathComponent
        return Stub(data: data(name == "operations" ? "operation" : name))
    }
    static func preferences(revision: String, poll: Int, hidden: [String] = []) -> Data {
        var envelope = document["preferences"] as! [String: Any]
        var prefs = envelope["data"] as! [String: Any]
        var values = prefs["values"] as! [String: Any]
        values["poll_ms"] = poll; values["hidden_panels"] = hidden
        prefs["revision"] = revision; prefs["values"] = values; envelope["data"] = prefs
        return try! JSONSerialization.data(withJSONObject: envelope)
    }
    static func operation(id: String, state: String = "succeeded", cursor: Int = 1) -> Data {
        var row = document["operation"] as! [String: Any]
        row["id"] = id; row["state"] = state; row["cursor"] = cursor
        row["events"] = [["seq": cursor, "text": "chunk\(cursor)\n"]]
        return try! JSONSerialization.data(withJSONObject: row)
    }
}
private struct Stub {
    var status = 200
    var data = Data()
    var delay = 0.0
    var error: Error?
    static func failure(_ status: Int, _ code: String) -> Stub {
        Stub(status: status, data: Data("{\"ok\":false,\"error\":\"\(code)\",\"output\":\"test refusal\"}".utf8))
    }
}
private final class ManagementStub: URLProtocol, @unchecked Sendable {
    static let lock = NSLock()
    static var handler: (URLRequest) -> Stub = Fixture.reply
    static var requests: [URLRequest] = []
    private var stopped = false
    private let stopLock = NSRecursiveLock()
    static func configure(_ handler: @escaping (URLRequest) -> Stub = Fixture.reply) {
        lock.lock(); defer { lock.unlock() }; requests = []; self.handler = handler
    }
    static var recorded: [URLRequest] { lock.lock(); defer { lock.unlock() }; return requests }
    static func api(_ host: String) throws -> ManagementAPI {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ManagementStub.self]
        return try ManagementAPI(host: host, configuration: configuration)
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var request = request
        if request.httpBody == nil, let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var bytes = [UInt8](repeating: 0, count: 4096), data = Data()
            while stream.hasBytesAvailable {
                let count = stream.read(&bytes, maxLength: bytes.count)
                if count <= 0 { break }; data.append(contentsOf: bytes.prefix(count))
            }
            request.httpBody = data
        }
        Self.lock.lock(); Self.requests.append(request); let handler = Self.handler; Self.lock.unlock()
        let stub = handler(request)
        DispatchQueue.global().asyncAfter(deadline: .now() + stub.delay) {
            self.stopLock.lock(); defer { self.stopLock.unlock() }
            guard !self.stopped else { return }
            if let error = stub.error { self.client?.urlProtocol(self, didFailWithError: error); return }
            self.client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: stub.status, httpVersion: nil,
                headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad: stub.data)
            self.client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func stopLoading() { stopLock.lock(); stopped = true; stopLock.unlock() }
}

final class ManagementAPITests: XCTestCase {
    override func setUp() { ManagementStub.configure() }
    func testTailnetValidationAndInventorySurviveOfflineRelaunch() throws {
        for bad in ["http://g.tail1.ts.net", "https://owner@g.tail1.ts.net", "https://g.tail1.ts.net:444", "localhost", "example.com", "https://g.tail1.ts.net/?x=1", "https://g.tail1.ts.net/#token", "https://g.tail1.ts.net/other", "ts.net"] {
            XCTAssertNil(ManagementAPI.tailnetHost(bad), bad)
        }
        let plot = try XCTUnwrap(GravePlot(tailnetHost: "https://Grave.tail123.ts.net/grave/"))
        let restored = GravePlot.restore(try JSONEncoder().encode([plot]))
        XCTAssertEqual(restored.count, 1); XCTAssertFalse(restored[0].reachable); XCTAssertNil(restored[0].summary)
        XCTAssertEqual(restored[0].candidate.dns, "grave.tail123.ts.net")
        XCTAssertEqual(GravePlot.merge(saved: restored, discovered: []).first?.id, plot.id)
    }
    func testDiscoveryMergesManualHostWithoutChangingItsSelectionID() throws {
        let manual = try XCTUnwrap(GravePlot(tailnetHost: "grave.tail123.ts.net"))
        let candidate = try JSONDecoder().decode(GraveCandidate.self, from: Data(#"{"id":"node-1","dns":"grave.tail123.ts.net","name":"discovered"}"#.utf8))
        let summary = try XCTUnwrap(GraveSummary.decode(Data(#"{"product":"gravedecay","api_version":1,"observed_at":"2026-09-20T12:00:00Z","node":{"host":"grave","platform":"linux","mode":"developer","uptime_s":12},"resources":{},"activity":{"sessions_live":0,"sessions_frozen":0},"health":{"services_failed":0,"containers_problem":0},"links":{"dashboard":"/grave/"}}"#.utf8)))
        let merged = GravePlot.merge(saved: [manual], discovered: [GravePlot(candidate: candidate, summary: summary)])
        XCTAssertEqual(merged.count, 1); XCTAssertEqual(merged.first?.id, manual.id)
        XCTAssertTrue(merged.first?.reachable == true)
        XCTAssertFalse(GravePlot.restore(try JSONEncoder().encode(merged))[0].reachable)
    }

    func testTypedResourcesUseOnlySelectedHTTPSHostAndProtocolHeader() async throws {
        let api = try ManagementStub.api("one.tail123.ts.net")
        let caps = try await api.capabilities()
        XCTAssertTrue(caps.supports("system")); XCTAssertTrue(caps.supports("preferences", method: "POST"))
        XCTAssertFalse(caps.supports("unknown")); XCTAssertEqual(caps.durableActions, ["doctor", "reboot"])
        let system = try await api.resource("system", as: GraveSystem.self)
        XCTAssertEqual(system.data?.cpu.usage_percent, 23); XCTAssertNil(system.data?.temperature.cpu_celsius)
        let services = try await api.resource("services", as: [GraveService].self)
        let containers = try await api.resource("containers", as: [GraveContainer].self)
        let sessions = try await api.resource("sessions", as: [GraveSession].self)
        let repos = try await api.resource("repositories", as: [GraveRepository].self)
        XCTAssertEqual(services.data?.first?.id, "t3code"); XCTAssertEqual(containers.data?.first?.name, "redis")
        XCTAssertEqual(sessions.data?.first?.window_count, 2); XCTAssertEqual(repos.data?.first?.changed_files, 3)
        for request in ManagementStub.recorded {
            XCTAssertEqual(request.url?.host, api.host); XCTAssertEqual(request.url?.scheme, "https")
            XCTAssertTrue(request.url!.path.hasPrefix("/grave/api/v1/"))
            XCTAssertEqual(request.value(forHTTPHeaderField: "X-Grave-Client"), "1")
            for header in ["Tailscale-User-Login", "Tailscale-User-Name", "Authorization", "Cookie", "Origin", "X-Grave-Local-Token"] {
                XCTAssertNil(request.value(forHTTPHeaderField: header), header)
            }
        }
    }
    func testOnlyChangedPreferencesAndOriginalRevisionArePosted() async throws {
        let api = try ManagementStub.api("one.tail123.ts.net")
        let reply = try await api.resource("preferences", as: GravePreferences.self)
        let base = try XCTUnwrap(reply.data); var values = base.values; values.poll_ms = 7777
        _ = try await api.savePreferences(base: base, values: values)
        let request = try XCTUnwrap(ManagementStub.recorded.last)
        let body = try JSONSerialization.jsonObject(with: XCTUnwrap(request.httpBody)) as! [String: Any]
        XCTAssertEqual(request.httpMethod, "POST"); XCTAssertEqual(body["revision"] as? String, base.revision)
        XCTAssertEqual(body["changes"] as? [String: Int], ["poll_ms": 7777])
    }
    func testDeniedOldRedirectAndMalformedResponsesNeverFallBack() async throws {
        for code in [401, 403, 404, 501, 302] {
            ManagementStub.configure { _ in .failure(code, "refused") }
            do { _ = try await ManagementStub.api("one.tail123.ts.net").capabilities(); XCTFail("Accepted \(code)") }
            catch let error as ManagementError { XCTAssertEqual(error.status, code) }
            XCTAssertEqual(ManagementStub.recorded.count, 1)
        }
        ManagementStub.configure { _ in Stub(data: Fixture.data("services")) }
        do { _ = try await ManagementStub.api("one.tail123.ts.net").resource("repositories", as: [GraveRepository].self); XCTFail("Wrong kind accepted") }
        catch { }
    }
    func testAdditiveCapabilitiesAndPausedResourceAreSafe() async throws {
        let unknown = try JSONDecoder().decode(GraveOperation.self, from: Fixture.operation(id: "1790000000-" + String(repeating: "a", count: 32), state: "future-state"))
        XCTAssertTrue(unknown.active); XCTAssertEqual(unknown.displayState, "unknown")
        var caps = Fixture.document["capabilities"] as! [String: Any]
        caps["future"] = true; caps.removeValue(forKey: "resource_contract"); caps.removeValue(forKey: "operations")
        ManagementStub.configure { _ in Stub(data: try! JSONSerialization.data(withJSONObject: caps)) }
        let old = try await ManagementStub.api("one.tail123.ts.net").capabilities()
        XCTAssertFalse(old.supports("system")); XCTAssertTrue(old.durableActions.isEmpty)
        ManagementStub.configure { _ in Stub(data: Data(#"{"api_version":1,"kind":"repositories","status":"paused","data":null,"observed_at":"2026-09-20T00:00:00Z","truncated":false,"error":{"code":"gaming_mode","message":"Paused"},"future":true}"#.utf8)) }
        let paused = try await ManagementStub.api("one.tail123.ts.net").resource("repositories", as: [GraveRepository].self)
        XCTAssertEqual(paused.status, "paused"); XCTAssertNil(paused.data)
    }
}

#if os(macOS)
@MainActor
final class ManagementModelTests: XCTestCase {
    private var directory: URL!
    private var trackingURL: URL { directory.appendingPathComponent("operations.json") }
    override func setUp() async throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        ManagementStub.configure()
    }
    override func tearDown() async throws { try FileManager.default.removeItem(at: directory) }
    private func model() -> ManagementModel { ManagementModel(trackingURL: trackingURL, makeClient: ManagementStub.api) }

    func testSwitchDiscardsLateResourcesAndRetainsEachGravesDraft() async throws {
        let started = expectation(description: "A request started")
        ManagementStub.configure { request in
            var reply = Fixture.reply(request)
            if request.url?.host == "one.tail123.ts.net" { reply.delay = 0.1; if request.url?.lastPathComponent == "capabilities" { started.fulfill() } }
            return reply
        }
        let model = model(); model.select("one.tail123.ts.net")
        let old = Task { await model.refresh() }
        await fulfillment(of: [started], timeout: 3)
        model.select("two.tail123.ts.net"); await model.refresh(); await old.value
        XCTAssertEqual(model.host, "two.tail123.ts.net"); XCTAssertNotNil(model.system)
        XCTAssertFalse(ManagementStub.recorded.contains { $0.url?.host == "one.tail123.ts.net" && $0.url?.lastPathComponent == "system" })
        model.draft?.values.poll_ms = 8888
        model.select("one.tail123.ts.net"); XCTAssertNil(model.system); XCTAssertNil(model.draft)
        model.select("two.tail123.ts.net"); XCTAssertEqual(model.draft?.values.poll_ms, 8888)
        ManagementStub.configure { _ in Stub(error: URLError(.notConnectedToInternet)) }
        await model.refresh()
        XCTAssertNil(model.system); XCTAssertEqual(model.host, "two.tail123.ts.net"); XCTAssertFalse(model.canSave)
        XCTAssertEqual(model.draft?.values.poll_ms, 8888)
    }
    func testLatePrivateResourceCannotOverwriteNewSelectionEvenAfterReturningToSameHost() async throws {
        let started = expectation(description: "Private resource requested")
        ManagementStub.configure { request in
            var reply = Fixture.reply(request)
            if request.url?.host == "one.tail123.ts.net", request.url?.lastPathComponent == "system" {
                started.fulfill(); reply.delay = 0.2
            }
            return reply
        }
        let model = model(); model.select("one.tail123.ts.net")
        let old = Task { await model.refresh() }
        await fulfillment(of: [started], timeout: 3)
        model.select("two.tail123.ts.net"); model.select("one.tail123.ts.net")
        await old.value
        XCTAssertNil(model.system); XCTAssertNil(model.services); XCTAssertNil(model.draft)
    }

    func testLostPreferenceSuccessRequiresReadAndNeverBlindlyRetries() async throws {
        let model = model(); model.select("one.tail123.ts.net"); await model.refresh()
        model.draft?.values.poll_ms = 7777
        ManagementStub.configure { _ in Stub(error: URLError(.networkConnectionLost)) }
        await model.savePreferences(); await model.savePreferences()
        XCTAssertEqual(ManagementStub.recorded.count, 1)
        XCTAssertTrue(model.needsReview); XCTAssertEqual(model.draft?.values.poll_ms, 7777)
        ManagementStub.configure { _ in Stub(data: Fixture.preferences(revision: String(repeating: "c", count: 64), poll: 7777)) }
        await model.readCurrentPreferences(); try model.rebaseDraft()
        XCTAssertFalse(model.dirty); XCTAssertFalse(model.canSave)
    }

    func testConflictKeepsDraftAndExplicitRebasePreservesOtherWritersFields() async throws {
        let model = model(); model.select("one.tail123.ts.net"); await model.refresh()
        model.draft?.values.poll_ms = 7777
        ManagementStub.configure { _ in .failure(409, "revision_conflict") }
        await model.savePreferences()
        XCTAssertEqual(model.draft?.values.poll_ms, 7777); XCTAssertTrue(model.needsReview); XCTAssertFalse(model.canSave)
        XCTAssertNil(model.currentPreferences)
        ManagementStub.configure { _ in Stub(data: Fixture.preferences(revision: String(repeating: "b", count: 64), poll: 6000, hidden: ["services"])) }
        await model.readCurrentPreferences()
        XCTAssertEqual(model.draft?.values.poll_ms, 7777); XCTAssertTrue(model.needsReview)
        try model.rebaseDraft()
        XCTAssertEqual(model.draft?.values.hidden_panels, ["services"]); XCTAssertEqual(model.draft?.values.poll_ms, 7777)
        XCTAssertTrue(model.canSave)
        await model.savePreferences()
        let body = try JSONSerialization.jsonObject(with: XCTUnwrap(ManagementStub.recorded.last?.httpBody)) as! [String: Any]
        XCTAssertEqual(body["revision"] as? String, String(repeating: "b", count: 64))
        XCTAssertEqual(body["changes"] as? [String: Int], ["poll_ms": 7777])
    }
    func testLostPOSTSurvivesRelaunchAndRetriesOnlySameMissingUnacknowledgedID() async throws {
        let first = model(); first.select("one.tail123.ts.net"); await first.refresh()
        let path = trackingURL
        ManagementStub.configure { request in
            if request.httpMethod == "POST" {
                XCTAssertTrue(FileManager.default.fileExists(atPath: path.path), "Must persist before sending")
                return Stub(error: URLError(.networkConnectionLost))
            }
            return Fixture.reply(request)
        }
        await first.start("doctor")
        let saved = try XCTUnwrap(first.tracking); XCTAssertFalse(saved.acknowledged)
        let restored = model(); restored.select("one.tail123.ts.net")
        ManagementStub.configure(); await restored.refresh()
        ManagementStub.configure { request in
            if request.httpMethod == "GET" { return .failure(404, "not_found") }
            let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: String]
            XCTAssertEqual(body["id"], saved.id)
            return Stub(data: Fixture.operation(id: saved.id))
        }
        await restored.resume()
        XCTAssertEqual(ManagementStub.recorded.map(\.httpMethod), ["GET", "POST"])
        XCTAssertEqual(restored.tracking?.id, saved.id); XCTAssertTrue(restored.tracking?.acknowledged == true)
        XCTAssertEqual(restored.operationText, "chunk1\n")
        ManagementStub.configure { _ in .failure(404, "not_found") }
        await restored.resume()
        XCTAssertEqual(ManagementStub.recorded.map(\.httpMethod), ["GET"], "Acknowledged records must never be recreated")
        XCTAssertEqual(restored.tracking?.id, saved.id)
        let permissions = try FileManager.default.attributesOfItem(atPath: path.path)[.posixPermissions] as? Int
        XCTAssertEqual(permissions, 0o600)
    }
    func testSwitchDuringPOSTNeverMovesOperationToNewDestination() async throws {
        let model = model(); model.select("one.tail123.ts.net"); await model.refresh()
        let started = expectation(description: "POST started")
        ManagementStub.configure { request in
            let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: String]
            started.fulfill()
            return Stub(data: Fixture.operation(id: body["id"]!), delay: 0.1)
        }
        let operation = Task { await model.start("doctor") }
        await fulfillment(of: [started], timeout: 3)
        model.select("two.tail123.ts.net"); await operation.value
        XCTAssertNil(model.tracking); XCTAssertNil(model.operation); XCTAssertEqual(model.operationText, "")
        XCTAssertNotNil(model.tracked["one.tail123.ts.net"])
        XCTAssertTrue(ManagementStub.recorded.allSatisfy { $0.url?.host == "one.tail123.ts.net" })
    }
    func testProgressResumesWithCursorAndNeverPostsOnRefresh() async throws {
        let model = model(); model.select("one.tail123.ts.net"); await model.refresh()
        let operationID = "1790000000-" + String(repeating: "a", count: 32)
        ManagementStub.configure { request in
            if request.httpMethod == "POST" {
                let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: String]
                return Stub(data: Fixture.operation(id: body["id"]!, state: "running"))
            }
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!
            XCTAssertEqual(query.first { $0.name == "after" }?.value, "1")
            return Stub(data: Fixture.operation(id: query.first { $0.name == "id" }!.value!, cursor: 2))
        }
        await model.start("doctor")
        XCTAssertEqual(model.operationText, "chunk1\nchunk2\n"); XCTAssertTrue(model.tracking?.finished == true)
        XCTAssertNotEqual(model.tracking?.id, operationID)
        ManagementStub.configure(); await model.refresh()
        XCTAssertTrue(ManagementStub.recorded.allSatisfy { $0.httpMethod == "GET" && $0.url?.lastPathComponent != "operations" })
    }
    func testAcknowledgementWriteFailureNeverTurnsKnownOperationIntoANewStart() async throws {
        let model = model(); model.select("one.tail123.ts.net"); await model.refresh()
        let path = trackingURL
        ManagementStub.configure { request in
            let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: String]
            try! FileManager.default.removeItem(at: path)
            try! FileManager.default.createDirectory(at: path, withIntermediateDirectories: true)
            return Stub(data: Fixture.operation(id: body["id"]!))
        }
        await model.start("doctor")
        XCTAssertNotNil(model.storageError); XCTAssertTrue(model.tracking?.acknowledged == true)
        ManagementStub.configure { _ in .failure(404, "not_found") }
        await model.resume(); await model.start("doctor")
        XCTAssertEqual(ManagementStub.recorded.map(\.httpMethod), ["GET"])
    }

    func testUnreadableTrackingPreventsMutationAndResourceFailureIsIndependent() async throws {
        try Data("corrupt".utf8).write(to: trackingURL)
        let model = model(); model.select("one.tail123.ts.net")
        ManagementStub.configure { request in request.url?.lastPathComponent == "services" ? .failure(503, "unavailable") : Fixture.reply(request) }
        await model.refresh(); await model.start("doctor")
        XCTAssertNotNil(model.storageError); XCTAssertNotNil(model.system); XCTAssertNil(model.services)
        XCTAssertNotNil(model.errors["services"])
        XCTAssertTrue(ManagementStub.recorded.allSatisfy { $0.httpMethod == "GET" })
    }
}
#endif
