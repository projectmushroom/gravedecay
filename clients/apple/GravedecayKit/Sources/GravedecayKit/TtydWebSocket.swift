#if canImport(Darwin)
import Foundation

/// URLSessionWebSocketTask-backed transport for TtydSession.
///
/// Callbacks fire on URLSession's delegate queue — hop to your own
/// queue/actor before touching UI or a TtydSession.
public final class TtydWebSocket: NSObject {
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

    public func connect() {
        let task = session.webSocketTask(with: url, protocols: ["tty"])
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
            return .transport("\(error.domain)/\(error.code)")
        }
    }
}
#endif
