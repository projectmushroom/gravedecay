import Foundation

/// One gravedecay box, addressed by its tailnet HTTPS name. Knows the URL
/// layout that `tailscale serve` publishes (docs/PORTS.md): T3 at `/`, the
/// dashboard at `/grave/`, and the web terminal at `/term`.
public struct BoxConfig: Equatable, Sendable {
    public let host: String

    /// Accepts what a human pastes: "box.tail1234.ts.net",
    /// "https://box.tail1234.ts.net/grave/", trailing junk stripped.
    /// Returns nil when no plausible hostname remains.
    public init?(input: String) {
        let s = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let host = GraveDiscovery.dnsName(URL(string: s.contains("://") ? s : "https://" + s)?.host) else { return nil }
        self.host = host
    }

    public var baseURL: URL { URL(string: "https://\(host)/")! }
    /// The dashboard entry point — trailing slash matters (see README).
    public var dashboardURL: URL { URL(string: "https://\(host)/grave/")! }

    /// ttyd's token endpoint next to the websocket.
    public var terminalTokenURL: URL { URL(string: "https://\(host)/term/token")! }

    /// The terminal websocket. `arg` selects the tmux session via bin/webterm
    /// (`?arg=<session>`), matching the stock ttyd URL scheme.
    public func terminalWebSocketURL(arg: String? = nil) -> URL {
        var components = URLComponents()
        components.scheme = "wss"
        components.host = host
        components.path = "/term/ws"
        if let arg { components.queryItems = [URLQueryItem(name: "arg", value: arg)] }
        return components.url!
    }
}
