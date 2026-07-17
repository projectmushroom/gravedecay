import Foundation

/// Anything that can carry ttyd frames to the server (normally a websocket).
public protocol TtydConnection: AnyObject {
    func sendFrame(_ data: Data)
}

/// Receives decoded server messages. `output` hands over a `done` callback:
/// call it once the bytes have been consumed by the terminal — the session's
/// flow control counts outstanding chunks and pauses/resumes the server
/// exactly like web/term/app.js does for xterm.js's async write queue.
/// (SwiftTerm feeds synchronously, so calling `done` immediately is fine.)
public protocol TtydSessionDelegate: AnyObject {
    func ttydSession(_ session: TtydSession, output: Data, done: @escaping () -> Void)
    func ttydSession(_ session: TtydSession, setTitle title: String)
    func ttydSession(_ session: TtydSession, preferences: Data)
}

/// One websocket's worth of ttyd protocol state: the hello handshake, input
/// and resize encoding, and server flow control. Create a fresh session per
/// (re)connection — counters start at zero like a fresh page load.
///
/// Not thread-safe: confine each instance to a single queue or actor
/// (the app uses the main actor).
public final class TtydSession {
    /// Stock values from web/term/app.js.
    public struct FlowLimits: Sendable {
        public var limit: Int
        public var highWater: Int
        public var lowWater: Int

        public init(limit: Int = 100_000, highWater: Int = 10, lowWater: Int = 4) {
            self.limit = limit
            self.highWater = highWater
            self.lowWater = lowWater
        }
    }

    public weak var delegate: TtydSessionDelegate?
    private weak var connection: TtydConnection?
    private let flow: FlowLimits

    private var written = 0
    private var pending = 0
    private var paused = false

    public init(connection: TtydConnection, delegate: TtydSessionDelegate? = nil,
                flow: FlowLimits = FlowLimits()) {
        self.connection = connection
        self.delegate = delegate
        self.flow = flow
    }

    /// Send the hello frame. Call once, right after the websocket opens.
    public func start(token: String, columns: Int, rows: Int) {
        connection?.sendFrame(TtydProtocol.hello(token: token, columns: columns, rows: rows))
    }

    public func send(text: String) {
        connection?.sendFrame(TtydProtocol.inputFrame(text))
    }

    public func send(bytes: [UInt8]) {
        connection?.sendFrame(TtydProtocol.inputFrame(bytes))
    }

    public func resize(columns: Int, rows: Int) {
        connection?.sendFrame(TtydProtocol.resizeFrame(columns: columns, rows: rows))
    }

    /// Feed one incoming websocket frame.
    public func receive(_ frame: Data) {
        guard let message = TtydProtocol.parse(frame) else { return }
        switch message {
        case .output(let data):
            deliver(data)
        case .setWindowTitle(let title):
            delegate?.ttydSession(self, setTitle: title)
        case .preferences(let prefs):
            delegate?.ttydSession(self, preferences: prefs)
        }
    }

    // Flow control, ported line-for-line from app.js: only chunks that cross
    // the byte limit are tracked as pending; too many outstanding chunks
    // pauses the server until the terminal drains below the low-water mark.
    private func deliver(_ data: Data) {
        written += data.count
        guard written > flow.limit else {
            delegate?.ttydSession(self, output: data, done: {})
            return
        }
        written = 0
        pending += 1
        if pending > flow.highWater && !paused {
            paused = true
            connection?.sendFrame(TtydProtocol.pauseFrame)
        }
        delegate?.ttydSession(self, output: data, done: { [weak self] in
            self?.outputConsumed()
        })
    }

    private func outputConsumed() {
        pending -= 1
        if paused && pending < flow.lowWater {
            paused = false
            connection?.sendFrame(TtydProtocol.resumeFrame)
        }
    }
}
