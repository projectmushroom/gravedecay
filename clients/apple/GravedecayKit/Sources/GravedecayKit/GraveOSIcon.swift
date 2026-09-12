#if canImport(SwiftUI)
import SwiftUI

/// Local vector artwork shared by the native app and standalone menu client.
public struct GraveOSIcon: View {
    private let node: GraveSummary.Node?
    public init(_ node: GraveSummary.Node?) { self.node = node }
    public var body: some View {
        Image("os-" + GravePresentation.osIcon(node), bundle: .module)
            .resizable().renderingMode(.template).scaledToFit()
            .frame(width: 14, height: 14).accessibilityHidden(true)
    }
}

public struct GraveOSLabel: View {
    private let title: String
    private let node: GraveSummary.Node?
    public init(_ title: String, node: GraveSummary.Node?) { self.title = title; self.node = node }
    public var body: some View {
        Label { Text(title) } icon: { GraveOSIcon(node) }
    }
}
#endif
