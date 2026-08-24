import Foundation

public enum GitHubRemote {
    /// Accept only canonical github.com SSH/HTTPS origins; never turn an
    /// arbitrary Git remote into a clickable URL.
    public static func repository(_ remote: String?) -> String? {
        guard let remote else { return nil }
        let value = remote.trimmingCharacters(in: .whitespacesAndNewlines)
        let patterns = ["^git@github\\.com:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\\.git)?$", "^https://github\\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\\.git)?/?$"]
        for pattern in patterns {
            guard let regex = try? NSRegularExpression(pattern: pattern), let match = regex.firstMatch(in: value, range: NSRange(value.startIndex..., in: value)), let range = Range(match.range(at: 1), in: value) else { continue }
            return String(value[range])
        }
        return nil
    }
    public static func url(_ remote: String?) -> URL? { repository(remote).flatMap { URL(string: "https://github.com/\($0)") } }
}
