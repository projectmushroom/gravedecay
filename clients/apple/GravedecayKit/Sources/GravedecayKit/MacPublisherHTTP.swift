import Foundation

/// Pure request boundary for the optional native Mac publisher. The listener
/// supplies bytes; this type makes no sockets and works in Linux tests.
public enum MacPublisherHTTP {
    public static let maxRequestBytes = 4_096

    public static func response(request: Data, summary: Data) -> Data {
        guard request.count <= maxRequestBytes, let text = String(data: request, encoding: .utf8),
              let end = text.range(of: "\r\n\r\n"), end.upperBound == text.endIndex else { return reply(400) }
        guard let firstBreak = text.range(of: "\r\n") else { return reply(400) }
        let first = text[..<firstBreak.lowerBound].split(separator: " ")
        let lower = text.lowercased()
        guard first.count == 3, first[2].hasPrefix("HTTP/"),
              first[0] == "GET" || first[0] == "HEAD" else { return reply(405) }
        guard !lower.contains("\r\ncontent-length:"), !lower.contains("\r\ntransfer-encoding:") else { return reply(405) }
        let body: Data
        switch first[1] {
        case "/healthz": body = Data("{\"ok\":true}\n".utf8)
        case "/api/v1/summary": body = summary
        default: return reply(404)
        }
        return reply(200, body: first[0] == "HEAD" ? Data() : body, length: body.count)
    }

    private static func reply(_ status: Int, body: Data = Data(), length: Int? = nil) -> Data {
        let phrase = status == 200 ? "OK" : status == 404 ? "Not Found" : status == 405 ? "Method Not Allowed" : "Bad Request"
        return Data("HTTP/1.1 \(status) \(phrase)\r\nContent-Type: application/json\r\nCache-Control: no-store\r\nContent-Length: \(length ?? body.count)\r\nConnection: close\r\n\r\n".utf8) + body
    }
}

public enum MacPublisherSummary {
    public static func data(host: String = "Mac", uptime: Double? = nil, cpu: Double? = nil, memory: Double? = nil, disk: Double? = nil) -> Data {
        let resources: [String: Any] = ["cpu_pct": cpu ?? NSNull(), "memory_pct": memory ?? NSNull(), "disk_pct": disk ?? NSNull(), "cpu_temp_c": NSNull(), "gpu_temp_c": NSNull()]
        let value: [String: Any] = [
            "product": "gravedecay", "api_version": 1, "observed_at": ISO8601DateFormatter().string(from: .now),
            "node": ["host": host, "platform": "macos", "mode": "companion", "uptime_s": uptime.map { $0 as Any } ?? NSNull()],
            "resources": resources, "activity": ["sessions_live": 0, "sessions_frozen": 0],
            "health": ["services_failed": 0, "containers_problem": 0],
            "links": ["dashboard": NSNull(), "t3": NSNull(), "terminal": NSNull(), "network": NSNull()]
        ]
        return (try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])) ?? Data("{}".utf8)
    }
}
