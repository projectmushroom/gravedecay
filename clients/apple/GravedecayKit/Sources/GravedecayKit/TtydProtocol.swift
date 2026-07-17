import Foundation

/// The ttyd 1.7.7 websocket protocol, as spoken by the reference client
/// `web/term/app.js` (see docs/TERMINAL.md). Every frame starts with a
/// one-byte command; the first client frame is a JSON hello carrying the
/// auth token and the initial terminal size.
public enum TtydProtocol {
    // client → server commands
    public static let input: UInt8 = UInt8(ascii: "0")
    public static let resize: UInt8 = UInt8(ascii: "1")
    public static let pause: UInt8 = UInt8(ascii: "2")
    public static let resume: UInt8 = UInt8(ascii: "3")

    public enum ServerMessage: Equatable {
        case output(Data)            // '0' — terminal bytes
        case setWindowTitle(String)  // '1'
        case preferences(Data)       // '2' — JSON blob of the server's -t flags
    }

    public static let pauseFrame = Data([pause])
    public static let resumeFrame = Data([resume])

    /// The initial frame sent right after the websocket opens. ttyd 1.7.7
    /// reads AuthToken, columns, and rows from this JSON.
    public static func hello(token: String, columns: Int, rows: Int) -> Data {
        struct Hello: Encodable {
            let AuthToken: String
            let columns: Int
            let rows: Int
        }
        // Encoding Hello cannot fail; fall back to an empty-token hello anyway.
        return (try? JSONEncoder().encode(Hello(AuthToken: token, columns: columns, rows: rows)))
            ?? Data("{\"AuthToken\":\"\"}".utf8)
    }

    public static func inputFrame(_ text: String) -> Data {
        var frame = Data([input])
        frame.append(contentsOf: Array(text.utf8))
        return frame
    }

    public static func inputFrame(_ bytes: [UInt8]) -> Data {
        var frame = Data([input])
        frame.append(contentsOf: bytes)
        return frame
    }

    public static func resizeFrame(columns: Int, rows: Int) -> Data {
        struct Resize: Encodable {
            let columns: Int
            let rows: Int
        }
        var frame = Data([resize])
        frame.append((try? JSONEncoder().encode(Resize(columns: columns, rows: rows))) ?? Data())
        return frame
    }

    public static func parse(_ frame: Data) -> ServerMessage? {
        guard let command = frame.first else { return nil }
        let payload = Data(frame.dropFirst())
        switch command {
        case UInt8(ascii: "0"):
            return .output(payload)
        case UInt8(ascii: "1"):
            return .setWindowTitle(String(decoding: payload, as: UTF8.self))
        case UInt8(ascii: "2"):
            return .preferences(payload)
        default:
            return nil
        }
    }
}
