#if canImport(Darwin)
import Foundation

/// URLSessionWebSocketTask-backed transport for TtydSession. The URLSession
/// is injected so the caller can route it through an embedded-tailnet SOCKS
/// proxy (URLSessionConfiguration.proxyConfigurations) or use a plain
/// session when the Tailscale VPN app provides connectivity.
///
/// Callbacks fire on URLSession's delegate queue — hop to your own
/// queue/actor before touching UI or a TtydSession.
public final class TtydWebSocket: NSObject, TtydConnection {
    public var onOpen: (() -> Void)?
    public var onFrame: ((Data) -> Void)?
    public var onClose: ((Error?) -> Void)?

    private var task: URLSessionWebSocketTask?
    private let session: URLSession
    private let url: URL
    private var closed = false

    public init(url: URL, session: URLSession = .shared) {
        self.url = url
        self.session = session
        super.init()
    }

    // URLSession does not promise the Origin header a browser supplies.
    // Keep only the endpoint's HTTP origin, without a path or credentials.
    static func request(for url: URL) -> URLRequest {
        var origin = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        origin.scheme = url.scheme == "wss" ? "https" : "http"
        origin.user = nil
        origin.password = nil
        origin.path = ""
        origin.query = nil
        origin.fragment = nil
        if (origin.scheme == "https" && origin.port == 443) ||
           (origin.scheme == "http" && origin.port == 80) { origin.port = nil }
        var request = URLRequest(url: url)
        request.setValue(origin.string!, forHTTPHeaderField: "Origin")
        request.setValue("tty", forHTTPHeaderField: "Sec-WebSocket-Protocol")
        return request
    }

    public func connect() {
        let task = session.webSocketTask(with: Self.request(for: url))
        self.task = task
        task.resume()
        receiveLoop(task)
        // The injected session may carry its own delegate, so the per-task
        // didOpen callback isn't available; a ping round-trip only completes
        // after the handshake, which makes it a reliable open signal.
        task.sendPing { [weak self] error in
            guard let self else { return }
            if error == nil { self.onOpen?() } else { self.finish(error) }
        }
    }

    public func sendFrame(_ data: Data) {
        task?.send(.data(data)) { [weak self] error in
            if let error { self?.finish(error) }
        }
    }

    public func close() {
        closed = true
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
    }

    private func receiveLoop(_ task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(.data(let data)):
                self.onFrame?(data)
                self.receiveLoop(task)
            case .success(.string(let text)):
                self.onFrame?(Data(text.utf8))
                self.receiveLoop(task)
            case .success:
                self.receiveLoop(task)
            case .failure(let error):
                self.finish(error)
            }
        }
    }

    private func finish(_ error: Error?) {
        guard !closed else { return }
        closed = true
        task?.cancel()
        task = nil
        onClose?(error)
    }
}

/// GET /term/token. An empty token is valid for ttyd without `-c`, but a
/// failed or malformed HTTP request is not silently treated as one.
public enum TerminalToken {
    public static func fetch(from url: URL, session: URLSession = .shared) async -> TerminalTokenResult {
        do {
            let (data, response) = try await session.data(from: url)
            guard let http = response as? HTTPURLResponse else { return .invalidResponse }
            return TerminalTokenResponse.classify(data: data, statusCode: http.statusCode)
        } catch {
            let error = error as NSError
            let domain = error.domain.prefix(64).filter { $0.isLetter || $0.isNumber || $0 == "." || $0 == "_" || $0 == "-" }
            return .transport("\(domain)/\(error.code)")
        }
    }
}
#endif
