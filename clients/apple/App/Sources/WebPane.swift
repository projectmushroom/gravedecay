import SwiftUI
import WebKit
import Network

/// A WKWebView pinned to one of the box's surfaces (T3 at /, dashboard at
/// /grave/). With an embedded-tailnet proxy, all webview traffic is routed
/// through the loopback SOCKS5 endpoint via
/// WKWebsiteDataStore.proxyConfigurations — the same mechanism TailscaleKit
/// applies to URLSessions. The default (persistent) data store keeps T3's
/// login cookies across launches.
struct WebPane {
    let url: URL
    let proxy: SocksProxy?

    private func makeWebView() -> WKWebView {
        let configuration = WKWebViewConfiguration()
        let store = WKWebsiteDataStore.default()
        if let proxy {
            store.proxyConfigurations = [AppModel.proxyConfiguration(for: proxy)]
        }
        configuration.websiteDataStore = store

        let webView = WKWebView(frame: .zero, configuration: configuration)
        #if os(iOS)
        webView.allowsBackForwardNavigationGestures = true
        #endif
        #if DEBUG
        webView.isInspectable = true
        #endif
        webView.load(URLRequest(url: url))
        return webView
    }
}

#if os(iOS)
extension WebPane: UIViewRepresentable {
    func makeUIView(context: Context) -> WKWebView { makeWebView() }
    func updateUIView(_ webView: WKWebView, context: Context) {}
}
#else
extension WebPane: NSViewRepresentable {
    func makeNSView(context: Context) -> WKWebView { makeWebView() }
    func updateNSView(_ webView: WKWebView, context: Context) {}
}
#endif
