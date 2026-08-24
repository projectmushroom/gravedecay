import Foundation

public struct GraveCandidate: Identifiable, Equatable, Sendable {
    public let id: String
    public let dns: String
    public let name: String
}

public enum GraveDiscovery {
    public static func dnsName(_ value: String?) -> String? {
        var name = (value ?? "").lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
        if name.hasSuffix(".") { name.removeLast() }
        let labels = name.split(separator: ".", omittingEmptySubsequences: false)
        guard !name.isEmpty, name.count <= 253, labels.count >= 2,
              labels.allSatisfy({ label in
                  guard !label.isEmpty, label.count <= 63,
                        asciiAlphanumeric(label.utf8.first!),
                        asciiAlphanumeric(label.utf8.last!) else { return false }
                  return label.utf8.allSatisfy { asciiAlphanumeric($0) || $0 == 45 }
              }), let suffix = labels.last, suffix.count <= 63,
              suffix.utf8.allSatisfy({ $0 >= 97 && $0 <= 122 }) else { return nil }
        return name
    }

    private static func asciiAlphanumeric(_ value: UInt8) -> Bool {
        (value >= 97 && value <= 122) || (value >= 48 && value <= 57)
    }

    public static func candidates(statusData: Data) -> [GraveCandidate] {
        guard let root = try? JSONSerialization.jsonObject(with: statusData) as? [String: Any] else { return [] }
        var nodes = [root["Self"]].compactMap { $0 as? [String: Any] }
        if let peers = root["Peer"] as? [String: Any] { nodes += peers.values.compactMap { $0 as? [String: Any] } }
        var ids = Set<String>()
        return nodes.compactMap { node in
            guard node["Online"] as? Bool == true,
                  let dns = dnsName(node["DNSName"] as? String),
                  let id = ["ID", "StableID", "NodeID"].compactMap({ node[$0] as? String }).first(where: { !$0.isEmpty }),
                  ids.insert(id).inserted else { return nil }
            return GraveCandidate(id: id, dns: dns, name: (node["HostName"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? dns)
        }.sorted {
            let order = $0.name.localizedCaseInsensitiveCompare($1.name)
            return order == .orderedAscending || (order == .orderedSame && $0.id < $1.id)
        }
    }
}

public struct GraveSummary: Codable, Equatable, Sendable {
    public struct Node: Codable, Equatable, Sendable { public let host, platform, mode: String; public let uptime_s: Double? }
    public struct Resources: Codable, Equatable, Sendable { public let cpu_pct, memory_pct, disk_pct, cpu_temp_c, gpu_temp_c: Double? }
    public struct Activity: Codable, Equatable, Sendable { public let sessions_live, sessions_frozen: Int }
    public struct Health: Codable, Equatable, Sendable { public let services_failed, containers_problem: Int }
    public struct Links: Codable, Equatable, Sendable { public let dashboard, t3, terminal, network: String? }
    public let product: String
    public let api_version: Int
    public let observed_at: Date?
    public let node: Node
    public let resources: Resources
    public let activity: Activity
    public let health: Health
    public let links: Links

    public static func decode(_ data: Data) -> GraveSummary? {
        guard data.count <= 65_536 else { return nil }
        let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
        guard let result = try? decoder.decode(GraveSummary.self, from: data), result.product == "gravedecay", result.api_version == 1 else { return nil }
        return result
    }

    public var problems: Int { max(0, health.services_failed) + max(0, health.containers_problem) }
}

public enum GravePresentation {
    public enum Condition: Equatable { case unreachable, warning, active, frozen, healthy }

    public static func condition(summary: GraveSummary?, reachable: Bool) -> Condition {
        guard reachable, let summary else { return .unreachable }
        if summary.problems > 0 { return .warning }
        if summary.activity.sessions_live > 0 { return .active }
        if summary.activity.sessions_frozen > 0 { return .frozen }
        return .healthy
    }

    public static func percent(_ value: Double?) -> String { value.map { String(format: "%.0f%%", $0) } ?? "—" }
    public static func temperature(_ value: Double?) -> String { value.map { String(format: "%.0f°C", $0) } ?? "—" }
    public static func uptime(_ seconds: Double?) -> String {
        guard let seconds else { return "—" }
        let value = max(0, Int(seconds)); let days = value / 86_400; let hours = (value % 86_400) / 3_600
        return days > 0 ? "\(days)d \(hours)h" : "\(hours)h \((value % 3_600) / 60)m"
    }
    public static func age(_ date: Date?, now: Date = .now) -> String {
        guard let date else { return "unknown" }
        let seconds = max(0, Int(now.timeIntervalSince(date)))
        if seconds < 5 { return "just now" }
        if seconds < 60 { return "\(seconds)s ago" }
        if seconds < 3_600 { return "\(seconds / 60)m ago" }
        if seconds < 86_400 { return "\(seconds / 3_600)h ago" }
        return "\(seconds / 86_400)d ago"
    }
    public static func link(host: String, path: String?) -> URL? {
        guard let host = GraveDiscovery.dnsName(host), let path,
              path.hasPrefix("/"), !path.hasPrefix("//"),
              !path.contains("\\"), !path.unicodeScalars.contains(where: { $0.value < 32 || $0.value == 127 }) else { return nil }
        var components = URLComponents(); components.scheme = "https"; components.host = host; components.percentEncodedPath = path
        return components.url
    }
}
