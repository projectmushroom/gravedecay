import Foundation

/// The native publisher never runs this command. It is displayed so a human
/// can make the explicit Tailscale Serve change after reviewing their routes.
public enum MacHostingPlan {
    public enum Preflight: Equatable { case attemptListener, existingCompanion }

    public static func preflight(legacyCompanionActive: Bool) -> Preflight {
        legacyCompanionActive ? .existingCompanion : .attemptListener
    }

    public static func manualServeCommand(port: Int = 4712) -> String? {
        guard (1...65_535).contains(port) else { return nil }
        return "tailscale serve --bg --https=443 --set-path=/grave http://127.0.0.1:\(port)"
    }
}
