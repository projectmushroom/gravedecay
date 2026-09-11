import Foundation

public enum MacHostIdentity {
    /// Exactly the signed-in Self identity, never a peer or the first account.
    public static func owner(_ data: Data) -> String? {
        guard let status = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              status["BackendState"] as? String == "Running",
              let own = status["Self"] as? [String: Any], let id = own["UserID"] as? NSNumber,
              let users = status["User"] as? [String: Any], let user = users[id.stringValue] as? [String: Any],
              let login = user["LoginName"] as? String, !login.isEmpty,
              login.range(of: "^[A-Za-z0-9._+@-]+$", options: .regularExpression) != nil else { return nil }
        return login
    }
}
