import AppKit
import CryptoKit

/// A disposable app exercising the production updater, never the installed app.
@main
struct UpdateSmoke {
    @MainActor static func main() throws {
        if CommandLine.arguments.contains("--generate-key") {
            let key = Curve25519.Signing.PrivateKey()
            let data = try JSONSerialization.data(withJSONObject: [
                "private": key.rawRepresentation.base64EncodedString(),
                "public": key.publicKey.rawRepresentation.base64EncodedString()])
            print(String(decoding: data, as: UTF8.self))
            return
        }
        let root = URL(fileURLWithPath: Bundle.main.object(forInfoDictionaryKey: "SmokeRoot") as! String)
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let updates = MacAppUpdates(directory: root.appendingPathComponent("updates"))
        updates.beforeRelaunch = { try? Data("stopped".utf8).write(to: root.appendingPathComponent("before-relaunch")) }
        updates.start()
        try Data(String(ProcessInfo.processInfo.processIdentifier).utf8).write(to: root.appendingPathComponent("pid"), options: .atomic)
        withExtendedLifetime(updates) { app.run() }
    }
}
