import Foundation

public struct GitHubRow: Equatable, Sendable, Identifiable {
    public let number: Int; public let title, state: String; public let url: URL
    public var id: String { "\(number)-\(url.absoluteString)" }
    public init(number: Int, title: String, state: String, url: URL) { self.number = number; self.title = title; self.state = state; self.url = url }
}

public struct CPUActivity: Equatable, Sendable {
    public let user, system, total: Double
}

public struct MemoryActivity: Equatable, Sendable {
    public let usedPercent: Double?
    public let compressedBytes: Int64?
}

public struct ThermalActivity: Equatable, Sendable {
    public let throttled: Bool
    public let speedLimit, availableCPUs: Int?
}

public struct SwapActivity: Equatable, Sendable {
    public let usedBytes, totalBytes: Int64
    public var usedPercent: Double { totalBytes > 0 ? Double(usedBytes) / Double(totalBytes) * 100 : 0 }
}

public enum NativeParsers {
    public static func cpuActivity(_ text: String) -> CPUActivity? {
        guard let values = captures(#"CPU usage:\s*([0-9]+(?:\.[0-9]+)?)% user,\s*([0-9]+(?:\.[0-9]+)?)% sys"#, in: text),
              values.count == 2, let user = Double(values[0]), let system = Double(values[1]) else { return nil }
        return CPUActivity(user: user, system: system, total: min(100, user + system))
    }
    public static func memoryActivity(pressure: String, vmStat: String) -> MemoryActivity {
        let free = captures(#"System-wide memory free percentage:\s*([0-9]+(?:\.[0-9]+)?)%"#, in: pressure)?.first.flatMap(Double.init)
        let pageSize = captures(#"page size of\s+([0-9]+) bytes"#, in: vmStat)?.first.flatMap(Int64.init) ?? 4096
        let pages = captures(#"Pages occupied by compressor:\s*([0-9]+)\."#, in: vmStat)?.first.flatMap(Int64.init)
        let compressed = pages.flatMap { value -> Int64? in let result = value.multipliedReportingOverflow(by: pageSize); return result.overflow ? nil : result.partialValue }
        return MemoryActivity(usedPercent: free.map { max(0, min(100, 100 - $0)) }, compressedBytes: compressed)
    }
    public static func thermalActivity(_ text: String) -> ThermalActivity {
        let speed = captures(#"(?m)^\s*CPU_Speed_Limit\s*=\s*([0-9]+)\s*$"#, in: text)?.first.flatMap(Int.init)
        let available = captures(#"(?m)^\s*CPU_Available_CPUs\s*=\s*([0-9]+)\s*$"#, in: text)?.first.flatMap(Int.init)
        let scheduler = captures(#"(?m)^\s*CPU_Scheduler_Limit\s*=\s*([0-9]+)\s*$"#, in: text)?.first.flatMap(Int.init)
        return ThermalActivity(throttled: [speed, scheduler].compactMap { $0 }.contains { $0 < 100 }, speedLimit: speed, availableCPUs: available)
    }
    public static func swapActivity(_ text: String) -> SwapActivity? {
        guard let values = captures(#"total\s*=\s*([0-9]+(?:\.[0-9]+)?)M\s+used\s*=\s*([0-9]+(?:\.[0-9]+)?)M"#, in: text),
              values.count == 2, let total = Double(values[0]), let used = Double(values[1]), total >= 0, used >= 0, used <= total,
              total <= Double(Int64.max) / 1_048_576, used <= Double(Int64.max) / 1_048_576 else { return nil }
        return SwapActivity(usedBytes: Int64(used * 1_048_576), totalBytes: Int64(total * 1_048_576))
    }
    public static func batteryPercent(_ text: String) -> Double? {
        captures(#"([0-9]+(?:\.[0-9]+)?)%;"#, in: text)?.first.flatMap(Double.init).map { max(0, min(100, $0)) }
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

    private static func captures(_ pattern: String, in text: String) -> [String]? {
        guard text.utf8.count <= 65_536, let regex = try? NSRegularExpression(pattern: pattern),
              let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) else { return nil }
        return (1..<match.numberOfRanges).compactMap { Range(match.range(at: $0), in: text).map { String(text[$0]) } }
    }
}
