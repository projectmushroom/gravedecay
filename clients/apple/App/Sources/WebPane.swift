// T3 and the legacy dashboard remain iOS-only web panes.  macOS opens those
// explicitly in the user's browser and never embeds a web dashboard.
#if os(iOS)
import SwiftUI
import WebKit

/// A WKWebView pinned to one of the box's surfaces (T3 at /, dashboard at
/// /grave/). The default (persistent) data store keeps T3's login cookies
/// across launches.
struct WebPane: UIViewRepresentable {
    let url: URL

    func makeUIView(context: Context) -> WKWebView {
        let webView = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        webView.allowsBackForwardNavigationGestures = true
        #if DEBUG
        webView.isInspectable = true
        #endif
        webView.load(URLRequest(url: url))
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {}
}
#endif
