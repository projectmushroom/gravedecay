import Foundation

public struct GitHubRow: Equatable, Sendable, Identifiable {
    public let number: Int; public let title, state, url: String
    public var id: String { "\(number)-\(url)" }
}

public enum NativeParsers {
    public static func cpuUsage(_ text: String) -> String? {
        guard let range = text.range(of: "CPU usage: [0-9.]+% user, [0-9.]+% sys", options: .regularExpression) else { return nil }
        return String(text[range]).replacingOccurrences(of: "CPU usage: ", with: "")
    }
    public static func githubRows(_ data: Data) -> [GitHubRow] {
        guard let rows = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return [] }
        return rows.prefix(3).compactMap { row in
            guard let number = row["number"] as? Int, let title = row["title"] as? String, let state = row["state"] as? String, let url = row["url"] as? String, URL(string: url)?.host == "github.com" else { return nil }
            return GitHubRow(number: number, title: title, state: state, url: url)
        }
    }
    public static func interfaceBytes(_ text: String) -> [(String, Int64, Int64)] {
        var seen = Set<String>(); return text.split(whereSeparator: \.isNewline).compactMap { line in
            let p = line.split(whereSeparator: \.isWhitespace); guard p.count > 9, !seen.contains(String(p[0])), let input = Int64(p[6]), let output = Int64(p[9]) else { return nil }; seen.insert(String(p[0])); return (String(p[0]), input, output)
        }
    }
}
