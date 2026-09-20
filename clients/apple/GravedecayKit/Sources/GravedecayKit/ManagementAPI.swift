import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// The shared v1 contract. Unknown response fields are ignored; availability comes
/// from the destination's capabilities, never a legacy or loopback fallback.
public struct ManagementCapabilities: Decodable, Sendable {
    public let product: String
    public let api_version: Int
    public let routes: [String: [String]]
    public let resource_contract: ResourceContract?
    public let operations: Operations?
    public struct ResourceContract: Decodable, Sendable {
        public let version: String
        public let resources: [String]
    }
    public struct Operations: Decodable, Sendable {
        public let `protocol`: Int
        public let actions: [String]
    }
    public func supports(_ resource: String, method: String = "GET") -> Bool {
        resource_contract?.version.split(separator: ".").first == "1" &&
        resource_contract?.resources.contains(resource) == true &&
        routes[method]?.contains("resources/" + resource) == true
    }
    public var durableActions: [String] {
        guard operations?.protocol == 1, routes["GET"]?.contains("operations") == true,
              routes["POST"]?.contains("operations") == true else { return [] }
        return operations?.actions ?? []
    }
}

public struct ManagementResource<Value: Decodable & Sendable>: Decodable, Sendable {
    public let api_version: Int
    public let kind: String
    public let observed_at: String
    public let status: String
    public let data: Value?
    public let error: Problem?
    public let truncated: Bool
    public struct Problem: Decodable, Sendable { public let code: String; public let message: String }
}

public struct GraveSystem: Decodable, Sendable {
    public struct Identity: Decodable, Sendable { public let hostname, platform, os_name: String }
    public struct CPU: Decodable, Sendable { public let usage_percent: Double?; public let logical_count: Int? }
    public struct Memory: Decodable, Sendable {
        public let total_bytes, used_bytes: Int64?
        public let usage_percent: Double?
        public let percent_kind: String
    }
    public struct Disk: Decodable, Identifiable, Sendable {
        public let id, mountpoint: String
        public let total_bytes, used_bytes: Int64?
        public let usage_percent: Double?
    }
    public struct Temperature: Decodable, Sendable { public let cpu_celsius, gpu_celsius: Double? }
    public let identity: Identity
    public let uptime_seconds: Double?
    public let cpu: CPU
    public let memory: Memory
    public let load_average: [Double?]
    public let disks: [Disk]
    public let temperature: Temperature
}
public struct GraveService: Decodable, Identifiable, Sendable {
    public let id, manager: String
    public let state, detail: String?
}
public struct GraveContainer: Decodable, Identifiable, Sendable {
    public let id, name: String
    public let state, status_message, project: String?
}
public struct GraveSession: Decodable, Identifiable, Sendable {
    public let id, name: String
    public let window_count: Int?
    public let attached: Bool?
    public let frozen: Bool
    public let activity_label: String?
}
public struct GraveRepository: Decodable, Identifiable, Sendable {
    public let id, name: String
    public let branch: String?
    public let changed_files: Int?
    public let last_commit_subject: String?
}
public struct GravePreferences: Codable, Equatable, Sendable {
    public let revision: String
    public var values: Values
    public struct Values: Codable, Equatable, Sendable {
        public var panel_order, hidden_panels, hidden_apps, newtab_apps, modal_apps, yolo_apps: [String]
        public var custom_apps: [Tile]
        public var t3_tile: String
        public var poll_ms: Int
    }
    public struct Tile: Codable, Equatable, Sendable {
        public var name, url: String
        public init(name: String, url: String) { self.name = name; self.url = url }
    }
}
public struct GraveOperation: Decodable, Sendable {
    public struct Event: Decodable, Sendable { public let seq: Int; public let text: String }
    public let id, action, state: String
    public let cursor: Int
    public let events: [Event]
    public let truncated: Bool
    public let message: String
    public let exit_code: Int?
    public var active: Bool { !["succeeded", "failed", "timed_out", "interrupted"].contains(state) }
    public var displayState: String {
        ["queued", "running", "succeeded", "failed", "timed_out", "interrupted"].contains(state) ? state : "unknown"
    }
}

public struct ManagementError: Error, LocalizedError, Sendable {
    public let status: Int
    public let code: String
    public let message: String
    public var errorDescription: String? { message }
    public init(_ message: String, status: Int = 0, code: String = "client") {
        self.message = message; self.status = status; self.code = code
    }
}

public final class ManagementAPI: Sendable {
    public let host: String
    private let session: URLSession

    public static func tailnetHost(_ input: String) -> String? {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URLComponents(string: text.contains("://") ? text : "https://" + text),
              url.scheme == "https", url.user == nil, url.password == nil, url.port == nil,
              url.query == nil, url.fragment == nil, ["", "/", "/grave", "/grave/"].contains(url.path),
              let host = GraveDiscovery.dnsName(url.host), host.hasSuffix(".ts.net"),
              host.split(separator: ".").count >= 4 else { return nil }
        return host
    }
    public init(host: String, configuration: URLSessionConfiguration = .ephemeral) throws {
        guard let host = Self.tailnetHost(host) else { throw ManagementError("Use an HTTPS Tailscale name, such as grave.tail123.ts.net.") }
        self.host = host
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.urlCredentialStorage = nil
        configuration.urlCache = nil
        configuration.timeoutIntervalForRequest = 15
        configuration.timeoutIntervalForResource = 30
        session = URLSession(configuration: configuration, delegate: ManagementRedirects(), delegateQueue: nil)
    }
    deinit { session.invalidateAndCancel() }

    public func capabilities() async throws -> ManagementCapabilities {
        let result: ManagementCapabilities = try await request("capabilities")
        guard result.product == "gravedecay", result.api_version == 1 else { throw ManagementError("This grave has an incompatible management API.") }
        return result
    }
    public func resource<T: Decodable & Sendable>(_ name: String, as: T.Type) async throws -> ManagementResource<T> {
        let result: ManagementResource<T> = try await request("resources/" + name)
        guard result.api_version == 1, result.kind == name else { throw ManagementError("Unexpected resource contract for \(name).") }
        return result
    }
    public func savePreferences(base: GravePreferences, values: GravePreferences.Values) async throws -> ManagementResource<GravePreferences> {
        let encoder = JSONEncoder()
        let old = try JSONSerialization.jsonObject(with: encoder.encode(base.values)) as! [String: NSObject]
        let new = try JSONSerialization.jsonObject(with: encoder.encode(values)) as! [String: NSObject]
        let changes = new.filter { old[$0.key] != $0.value }
        let body = try JSONSerialization.data(withJSONObject: ["revision": base.revision, "changes": changes])
        guard body.count <= 65_536 else { throw ManagementError("Preferences exceed the 64 KiB request limit.") }
        let result: ManagementResource<GravePreferences> = try await request("resources/preferences", body: body)
        guard result.api_version == 1, result.kind == "preferences", result.status == "ready", result.data != nil else { throw ManagementError("Preference save could not be verified. Read current values before retrying.") }
        return result
    }
    public func operation(id: String, after: Int = 0) async throws -> GraveOperation {
        try await request("operations", query: [.init(name: "id", value: id), .init(name: "after", value: String(after))])
    }
    public func start(id: String, action: String) async throws -> GraveOperation {
        try await request("operations", body: JSONSerialization.data(withJSONObject: ["id": id, "action": action]))
    }
    private func request<T: Decodable>(_ path: String, query: [URLQueryItem] = [], body: Data? = nil) async throws -> T {
        var url = URLComponents(string: "https://\(host)/grave/api/v1/\(path)")!
        if !query.isEmpty { url.queryItems = query }
        var request = URLRequest(url: url.url!)
        request.httpMethod = body == nil ? "GET" : "POST"
        request.httpBody = body
        request.setValue("1", forHTTPHeaderField: "X-Grave-Client")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if body != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        let data: Data
        let response: URLResponse
        #if canImport(FoundationNetworking)
        (data, response) = try await session.data(for: request)
        #else
        let (bytes, reply) = try await session.bytes(for: request)
        response = reply
        var received = Data()
        for try await byte in bytes {
            received.append(byte)
            if received.count > 2_097_152 { throw ManagementError("Management response exceeds 2 MiB.") }
        }
        data = received
        #endif
        guard data.count <= 2_097_152, let http = response as? HTTPURLResponse else { throw ManagementError("Invalid management response.") }
        guard (200..<300).contains(http.statusCode) else {
            let error = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let code = error?["error"] as? String ?? "http_\(http.statusCode)"
            let detail: String
            switch http.statusCode {
            case 401, 403: detail = "Owner access denied. Check this Mac's Tailscale identity on the selected grave."
            case 404 where code == "not_found": detail = "Saved operation is missing or expired on this grave."
            case 404, 501: detail = "This grave does not support the requested management API. Update it or open its dashboard."
            case 300..<400: detail = "Redirect refused. Requests must stay on the selected grave."
            default: detail = error?["output"] as? String ?? "Management request failed (HTTP \(http.statusCode))."
            }
            throw ManagementError(detail, status: http.statusCode, code: code)
        }
        do { return try JSONDecoder().decode(T.self, from: data) }
        catch { throw ManagementError("The selected grave returned an incompatible response.") }
    }
}
private final class ManagementRedirects: NSObject, URLSessionTaskDelegate, Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) { completionHandler(nil) }
}
