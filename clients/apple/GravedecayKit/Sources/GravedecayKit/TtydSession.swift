import Foundation

/// One websocket's worth of ttyd protocol state: the hello handshake, input
/// and resize encoding, and server flow control. Create a fresh session per
/// (re)connection — counters start at zero like a fresh page load.
///
/// `onOutput` hands over a `done` callback: call it once the bytes have been
/// consumed by the terminal — flow control counts outstanding chunks and
/// pauses/resumes the server exactly like web/term/app.js does for xterm.js's
/// async write queue. (SwiftTerm feeds synchronously, so calling `done`
/// immediately is fine.)
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

    public let sendFrame: (Data) -> Void
    public var onOutput: (Data, @escaping () -> Void) -> Void = { _, done in done() }
    private let flow: FlowLimits

    private var written = 0
    private var pending = 0
    private var paused = false

    public init(sendFrame: @escaping (Data) -> Void, flow: FlowLimits = FlowLimits()) {
        self.sendFrame = sendFrame
        self.flow = flow
    }

    /// Send the hello frame. Call once, right after the websocket opens.
    public func start(token: String, columns: Int, rows: Int) {
        sendFrame(TtydProtocol.hello(token: token, columns: columns, rows: rows))
    }

    public func send(bytes: [UInt8]) {
        sendFrame(TtydProtocol.inputFrame(bytes))
    }

    public func resize(columns: Int, rows: Int) {
        sendFrame(TtydProtocol.resizeFrame(columns: columns, rows: rows))
    }

    /// Feed one incoming websocket frame.
    public func receive(_ frame: Data) {
        guard let data = TtydProtocol.parse(frame) else { return }
        // Flow control, ported line-for-line from app.js: only chunks that cross
        // the byte limit are tracked as pending; too many outstanding chunks
        // pauses the server until the terminal drains below the low-water mark.
        written += data.count
        guard written > flow.limit else {
            onOutput(data, {})
            return
        }
        written = 0
        pending += 1
        if pending > flow.highWater && !paused {
            paused = true
            sendFrame(TtydProtocol.pauseFrame)
        }
        onOutput(data, { [weak self] in self?.outputConsumed() })
    }

    private func outputConsumed() {
        pending -= 1
        if paused && pending < flow.lowWater {
            paused = false
            sendFrame(TtydProtocol.resumeFrame)
        }
    }
}
