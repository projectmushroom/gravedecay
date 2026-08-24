import Foundation

public struct GitHubRow: Equatable, Sendable, Identifiable {
    public let number: Int; public let title, state: String; public let url: URL
    public var id: String { "\(number)-\(url.absoluteString)" }
    public init(number: Int, title: String, state: String, url: URL) { self.number = number; self.title = title; self.state = state; self.url = url }
}

public enum NativeParsers {
    public static func cpuUsage(_ text: String) -> String? {
        guard let range = text.range(of: "CPU usage: [0-9.]+% user, [0-9.]+% sys", options: .regularExpression) else { return nil }
        return String(text[range]).replacingOccurrences(of: "CPU usage: ", with: "")
    }
    public static func githubRows(_ data: Data) -> [GitHubRow] {
        guard let rows = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return [] }
        return rows.prefix(3).compactMap { row in
            guard let number = row["number"] as? Int, let title = row["title"] as? String, let state = row["state"] as? String, let raw = row["url"] as? String, let url = URL(string: raw), url.scheme == "https", url.host == "github.com" else { return nil }
            return GitHubRow(number: number, title: title, state: state, url: url)
        }
    }
    public static func githubRun(_ data: Data) -> GitHubRow? {
        guard data.count <= 65_536,
              let rows = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]],
              let row = rows.first,
              let id = row["databaseId"] as? Int,
              let raw = row["url"] as? String,
              let url = URL(string: raw), url.scheme == "https", url.host == "github.com" else { return nil }
        let title = ((row["displayTitle"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false ? row["displayTitle"] as? String : row["workflowName"] as? String) ?? "GitHub Actions"
        guard title.count <= 240 else { return nil }
        let conclusion = (row["conclusion"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let status = (row["status"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let state = conclusion.isEmpty ? status : conclusion
        guard !state.isEmpty, state.count <= 80 else { return nil }
        return GitHubRow(number: id, title: title, state: state, url: url)
    }
    public static func interfaceBytes(_ text: String) -> [(String, Int64, Int64)] {
        var totals = [String: (Int64, Int64)]()
        for line in text.split(whereSeparator: \.isNewline) {
            let p = line.split(whereSeparator: \.isWhitespace); guard p.count > 9 else { continue }
            let name = String(p[0]); guard (name.hasPrefix("en") || name.hasPrefix("utun")), p[2].hasPrefix("<Link#"), let input = Int64(p[6]), let output = Int64(p[9]), input + output > 0 else { continue }
            let old = totals[name] ?? (0, 0); totals[name] = (max(old.0, input), max(old.1, output))
        }
        return totals.map { ($0.key, $0.value.0, $0.value.1) }.sorted { $0.1 + $0.2 > $1.1 + $1.2 }
    }
}
