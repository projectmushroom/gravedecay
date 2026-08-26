import Foundation

public enum TerminalTokenResult: Equatable { case token(String), http(Int), invalidResponse, transport(String) }

public enum TerminalTokenResponse {
    public static func classify(data: Data, statusCode: Int) -> TerminalTokenResult {
        struct Reply: Decodable {
            let token: String?
            enum Keys: String, CodingKey { case token }
            init(from decoder: Decoder) throws {
                let values = try decoder.container(keyedBy: Keys.self)
                guard values.contains(.token) else { throw DecodingError.keyNotFound(Keys.token, .init(codingPath: [], debugDescription: "missing token")) }
                token = try values.decodeIfPresent(String.self, forKey: .token)
            }
        }
        guard (200..<300).contains(statusCode) else { return .http(statusCode) }
        guard data.count <= 8_192, let reply = try? JSONDecoder().decode(Reply.self, from: data) else { return .invalidResponse }
        return .token(reply.token ?? "")
    }
}
