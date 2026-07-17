import Foundation
import Network
import SwiftUI
import GravedecayKit

/// How the app reaches the box's tailnet name.
enum TailnetMode: String, CaseIterable, Identifiable {
    case system    // the Tailscale app's VPN carries the traffic
    case embedded  // in-app TailscaleKit node + loopback SOCKS5 proxy

    var id: String { rawValue }

    var label: String {
        switch self {
        case .system: return "Tailscale app (VPN)"
        case .embedded: return "Embedded (in-app node)"
        }
    }
}

/// The loopback SOCKS5 proxy vended by the embedded tsnet node
/// (username is always "tsnet" — see TailscaleKit's URLSession extension).
struct SocksProxy: Equatable, Hashable {
    let host: String
    let port: UInt16
    let credential: String
}

@MainActor
final class AppModel: ObservableObject {
    @Published var box: BoxConfig?
    @Published var mode: TailnetMode = .system
    @Published private(set) var proxy: SocksProxy?
    @Published private(set) var tailnetError: String?
    @Published private(set) var tailnetBusy = false
    @Published private(set) var urlSession: URLSession = .shared

    static var embeddedAvailable: Bool {
        #if canImport(TailscaleKit)
        return true
        #else
        return false
        #endif
    }

    private let defaults = UserDefaults.standard

    init() {
        if let host = defaults.string(forKey: "boxHost") {
            box = BoxConfig(input: host)
        }
        mode = TailnetMode(rawValue: defaults.string(forKey: "tailnetMode") ?? "") ?? .system
    }

    func save() {
        defaults.set(box?.host, forKey: "boxHost")
        defaults.set(mode.rawValue, forKey: "tailnetMode")
    }

    /// Join (or rejoin) the tailnet with the in-app node. The auth key is
    /// only needed for the first enrollment — the node identity persists in
    /// Application Support, so pass nil afterwards. The key is never stored.
    func startEmbedded(authKey: String?) async {
        #if canImport(TailscaleKit)
        tailnetBusy = true
        tailnetError = nil
        defer { tailnetBusy = false }
        do {
            proxy = try await EmbeddedTailnet.shared.start(
                hostName: "gravedecay-app",
                authKey: (authKey?.isEmpty ?? true) ? nil : authKey)
            rebuildSession()
        } catch {
            tailnetError = String(describing: error)
        }
        #else
        tailnetError = "This build does not embed TailscaleKit."
        #endif
    }

    // The terminal's token fetch and websocket must take the same path as
    // the webviews: through the SOCKS proxy when the embedded node is up.
    private func rebuildSession() {
        guard let proxy else {
            urlSession = .shared
            return
        }
        let configuration = URLSessionConfiguration.default
        configuration.proxyConfigurations = [Self.proxyConfiguration(for: proxy)]
        urlSession = URLSession(configuration: configuration)
    }

    // Pure helper — must stay callable from nonisolated contexts (WebPane
    // builds its WKWebView configuration outside the main-actor inference).
    nonisolated static func proxyConfiguration(for proxy: SocksProxy) -> ProxyConfiguration {
        let endpoint = NWEndpoint.hostPort(host: NWEndpoint.Host(proxy.host),
                                           port: NWEndpoint.Port(rawValue: proxy.port)!)
        let configuration = ProxyConfiguration(socksv5Proxy: endpoint)
        configuration.applyCredential(username: "tsnet", password: proxy.credential)
        return configuration
    }
}
