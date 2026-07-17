import SwiftUI
import SwiftTerm
import GravedecayKit

#if os(iOS)
import UIKit
#else
import AppKit
#endif

/// Drives one SwiftTerm view against the box's web terminal: fetches the
/// ttyd token, opens the websocket (through the tailnet-aware URLSession),
/// runs the TtydSession protocol state machine, and reconnects forever with
/// app.js's backoff — the session itself survives in tmux on the box.
///
/// Everything runs on the main thread: websocket callbacks hop to the main
/// actor before touching the session or the view.
final class TerminalController: NSObject {
    private let box: BoxConfig
    private let arg: String?
    private let urlSession: URLSession

    private var socket: TtydWebSocket?
    private var session: TtydSession?
    private var retry = 0
    private var opened = false
    private(set) weak var terminalView: TerminalView?

    init(box: BoxConfig, arg: String?, urlSession: URLSession) {
        self.box = box
        self.arg = arg
        self.urlSession = urlSession
    }

    func attach(_ view: TerminalView) {
        terminalView = view
        view.terminalDelegate = self
        connect()
    }

    func detach() {
        terminalView = nil
        socket?.close()
        socket = nil
        session = nil
    }

    private func connect() {
        Task { @MainActor [weak self] in
            guard let self, self.terminalView != nil else { return }
            let token = await TerminalToken.fetch(from: self.box.terminalTokenURL,
                                                  session: self.urlSession)
            self.open(token: token)
        }
    }

    private func open(token: String) {
        let socket = TtydWebSocket(url: box.terminalWebSocketURL(arg: arg),
                                   session: urlSession)
        let session = TtydSession(connection: socket, delegate: self)

        socket.onOpen = { [weak self] in
            Task { @MainActor in
                guard let self, let view = self.terminalView else { return }
                self.opened = true
                self.retry = 0
                // tmux new-session -A reattaches, but the old screen content
                // is stale after a reconnect — reset before the repaint.
                view.getTerminal().resetToInitialState()
                session.start(token: token,
                              columns: view.getTerminal().cols,
                              rows: view.getTerminal().rows)
            }
        }
        socket.onFrame = { frame in
            Task { @MainActor in session.receive(frame) }
        }
        socket.onClose = { [weak self] _ in
            Task { @MainActor in self?.scheduleReconnect() }
        }

        self.socket = socket
        self.session = session
        socket.connect()
    }

    private func scheduleReconnect() {
        socket = nil
        session = nil
        guard terminalView != nil else { return }
        // app.js: 300ms right after a healthy connection drops, else
        // exponential backoff capped at 10s. Reconnect forever.
        let delay = (opened && retry == 0) ? 0.3 : min(pow(2.0, Double(retry)), 10.0)
        opened = false
        retry += 1
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard let self, self.socket == nil else { return }
            self.connect()
        }
    }
}

extension TerminalController: TtydSessionDelegate {
    func ttydSession(_ session: TtydSession, output: Data, done: @escaping () -> Void) {
        terminalView?.feed(byteArray: ArraySlice([UInt8](output)))
        done() // SwiftTerm parses synchronously
    }

    func ttydSession(_ session: TtydSession, setTitle title: String) {}
    func ttydSession(_ session: TtydSession, preferences: Data) {}
}

extension TerminalController: TerminalViewDelegate {
    func send(source: TerminalView, data: ArraySlice<UInt8>) {
        session?.send(bytes: Array(data))
    }

    func sizeChanged(source: TerminalView, newCols: Int, newRows: Int) {
        session?.resize(columns: newCols, rows: newRows)
    }

    func setTerminalTitle(source: TerminalView, title: String) {}
    func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?) {}
    func scrolled(source: TerminalView, position: Double) {}
    func bell(source: TerminalView) {}
    func iTermContent(source: TerminalView, content: ArraySlice<UInt8>) {}
    func rangeChanged(source: TerminalView, startY: Int, endY: Int) {}

    func requestOpenLink(source: TerminalView, link: String, params: [String: String]) {
        guard let url = URL(string: link) else { return }
        #if os(iOS)
        UIApplication.shared.open(url)
        #else
        NSWorkspace.shared.open(url)
        #endif
    }

    // OSC 52 — tmux `set-clipboard on` emits this on every copy-mode copy;
    // forward to the system clipboard (same behavior web/term/app.js adds).
    // Clipboard *reads* are never answered, so nothing running in the
    // terminal can exfiltrate the clipboard.
    func clipboardCopy(source: TerminalView, content: Data) {
        let text = String(decoding: content, as: UTF8.self)
        #if os(iOS)
        UIPasteboard.general.string = text
        #else
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        #endif
    }
}

struct TerminalPane {
    let box: BoxConfig
    let urlSession: URLSession
    var arg: String? = nil

    final class Coordinator {
        let controller: TerminalController
        init(controller: TerminalController) { self.controller = controller }
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(controller: TerminalController(box: box, arg: arg, urlSession: urlSession))
    }

    private func makeView(_ coordinator: Coordinator) -> TerminalView {
        let view = TerminalView(frame: .zero)
        #if os(iOS)
        view.nativeBackgroundColor = UIColor(red: 0.05, green: 0.05, blue: 0.05, alpha: 1)
        #else
        view.nativeBackgroundColor = NSColor(red: 0.05, green: 0.05, blue: 0.05, alpha: 1)
        #endif
        coordinator.controller.attach(view)
        return view
    }
}

#if os(iOS)
extension TerminalPane: UIViewRepresentable {
    func makeUIView(context: Context) -> TerminalView { makeView(context.coordinator) }
    func updateUIView(_ view: TerminalView, context: Context) {}
    static func dismantleUIView(_ view: TerminalView, coordinator: Coordinator) {
        coordinator.controller.detach()
    }
}
#else
extension TerminalPane: NSViewRepresentable {
    func makeNSView(context: Context) -> TerminalView { makeView(context.coordinator) }
    func updateNSView(_ view: TerminalView, context: Context) {}
    static func dismantleNSView(_ view: TerminalView, coordinator: Coordinator) {
        coordinator.controller.detach()
    }
}
#endif
