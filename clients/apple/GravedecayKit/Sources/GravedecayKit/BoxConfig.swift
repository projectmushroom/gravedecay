import Foundation

/// One gravedecay box, addressed by its tailnet HTTPS name. Knows the URL
/// layout that `tailscale serve` publishes (docs/PORTS.md): T3 at `/`, the
/// dashboard at `/grave/`, and the web terminal at `/term`.
public struct BoxConfig: Equatable, Codable, Sendable {
    public let host: String
    public let terminalPath: String

    /// Accepts what a human pastes: "box.tail1234.ts.net",
    /// "https://box.tail1234.ts.net/grave/", trailing junk stripped.
    /// Returns nil when no plausible hostname remains.
    public init?(input: String) {
        var s = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        for scheme in ["https://", "http://", "wss://", "ws://"] where s.hasPrefix(scheme) {
            s = String(s.dropFirst(scheme.count))
        }
        if let slash = s.firstIndex(of: "/") { s = String(s[..<slash]) }
        if let colon = s.firstIndex(of: ":") { s = String(s[..<colon]) }
        guard !s.isEmpty, s.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "." || $0 == "-" })
        else { return nil }
        host = s
        terminalPath = "/term"
    }

    /// Builds a terminal target only after the summary has explicitly
    /// advertised the standard ttyd route.
    public init?(host: String, terminalPath: String?) {
        guard let host = GraveDiscovery.dnsName(host),
              let terminalPath = GravePresentation.terminalPath(terminalPath) else { return nil }
        self.host = host
        self.terminalPath = terminalPath.hasSuffix("/") ? String(terminalPath.dropLast()) : terminalPath
    }

    private enum CodingKeys: String, CodingKey { case host, terminalPath }
    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let host = try values.decode(String.self, forKey: .host)
        let terminalPath = try values.decodeIfPresent(String.self, forKey: .terminalPath) ?? "/term"
        guard let value = Self(host: host, terminalPath: terminalPath) else {
            throw DecodingError.dataCorruptedError(forKey: .host, in: values, debugDescription: "Invalid box")
        }
        self = value
    }

    public var baseURL: URL { URL(string: "https://\(host)/")! }
    public var t3URL: URL { baseURL }
    /// The dashboard entry point — trailing slash matters (see README).
    public var dashboardURL: URL { URL(string: "https://\(host)/grave/")! }
    public var terminalURL: URL { URL(string: "https://\(host)\(terminalPath)")! }

    /// ttyd's token endpoint next to the websocket.
    public var terminalTokenURL: URL { URL(string: "https://\(host)\(terminalPath)/token")! }

    /// The terminal websocket. `arg` selects the tmux session via bin/webterm
    /// (`?arg=<session>`), matching the stock ttyd URL scheme.
    public func terminalWebSocketURL(arg: String? = nil) -> URL {
        var components = URLComponents()
        components.scheme = "wss"
        components.host = host
        components.path = "\(terminalPath)/ws"
        if let arg { components.queryItems = [URLQueryItem(name: "arg", value: arg)] }
        return components.url!
    }
}
