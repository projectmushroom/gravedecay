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

    /// Terminal bytes from an output ('0') frame; title ('1') and preference
    /// ('2') frames are ignored.
    public static func parse(_ frame: Data) -> Data? {
        frame.first == input ? Data(frame.dropFirst()) : nil
    }
}
