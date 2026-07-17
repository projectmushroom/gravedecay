#if canImport(TailscaleKit)
import Foundation
import TailscaleKit

enum TailnetError: Error {
    case proxyUnavailable
}

/// Owns the in-app tsnet node. Node identity/state lives under Application
/// Support, so an auth key is required only for the very first enrollment;
/// later launches come up from the persisted state.
///
/// TailscaleKit is an optional build ingredient (`make project-embedded` in
/// clients/apple) — without it, this file compiles away and the app relies
/// on the Tailscale VPN app instead.
actor EmbeddedTailnet {
    static let shared = EmbeddedTailnet()

    private var node: TailscaleNode?

    func start(hostName: String, authKey: String?) async throws -> SocksProxy {
        if node == nil {
            let stateDir = try FileManager.default
                .url(for: .applicationSupportDirectory, in: .userDomainMask,
                     appropriateFor: nil, create: true)
                .appendingPathComponent("tsnet", isDirectory: true)
            try FileManager.default.createDirectory(at: stateDir,
                                                    withIntermediateDirectories: true)
            let config = Configuration(hostName: hostName,
                                       path: stateDir.path,
                                       authKey: authKey,
                                       controlURL: kDefaultControlURL,
                                       ephemeral: false)
            node = try TailscaleNode(config: config, logger: nil)
        }
        guard let node else { throw TailnetError.proxyUnavailable }

        try await node.up()
        let loopback = try await node.loopback()
        guard let ip = loopback.ip, let port = loopback.port,
              let port16 = UInt16(exactly: port)
        else { throw TailnetError.proxyUnavailable }

        return SocksProxy(host: ip, port: port16, credential: loopback.proxyCredential)
    }

    func stop() async throws {
        try await node?.close()
        node = nil
    }
}
#endif
