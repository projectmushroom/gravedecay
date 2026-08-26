import SwiftUI
import SwiftTerm
import GravedecayKit
#if os(macOS)
import OSLog
#endif

#if os(iOS)
import UIKit
#else
import AppKit
#endif

final class TerminalStatus: ObservableObject {
    enum State: String { case unavailable = "UNAVAILABLE", fetchingToken = "FETCHING TOKEN", connecting = "CONNECTING", connected = "CONNECTED", reconnecting = "RECONNECTING", error = "ERROR" }
    @Published private(set) var state: State = .unavailable
    @Published private(set) var failureCount = 0
    @Published private(set) var reconnectCount = 0
    @Published private(set) var retryID = 0

    #if os(macOS)
    private let logger = Logger(subsystem: Bundle.main.bundleIdentifier ?? "com.projectmushroom.gravedecay", category: "terminal")
    #endif

    func transition(_ value: State) {
        state = value
        #if os(macOS)
        logger.info("terminal state=\(value.rawValue, privacy: .public) failures=\(self.failureCount) reconnects=\(self.reconnectCount)")
        #endif
    }
    func failed() {
        failureCount += 1
        transition(.error)
    }
    func reconnecting() { reconnectCount += 1; transition(.reconnecting) }
    func retry() { retryID &+= 1; transition(.reconnecting) }
    var diagnostics: String { "TERMINAL STATE: \(state.rawValue)\nFAILURES: \(failureCount)\nRECONNECTS: \(reconnectCount)" }
}

/// Drives one SwiftTerm view against the box's web terminal: fetches the
/// ttyd token, opens the websocket (through the tailnet-aware URLSession),
/// and runs the TtydSession protocol state machine. Secret values and output
/// never leave the terminal path or diagnostics.
///
/// Everything runs on the main thread: websocket callbacks hop to the main
/// actor before touching the session or the view.
final class TerminalController: NSObject {
    private let box: BoxConfig
    private let arg: String?
    private let urlSession: URLSession
    private let status: TerminalStatus

    private var socket: TtydWebSocket?
    private var session: TtydSession?
    private var retry = 0
    private var opened = false
    private(set) weak var terminalView: TerminalView?

    init(box: BoxConfig, arg: String?, urlSession: URLSession, status: TerminalStatus = TerminalStatus()) {
        self.box = box
        self.arg = arg
        self.urlSession = urlSession
        self.status = status
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
        status.transition(.unavailable)
    }

    func retryNow() {
        socket?.close(); socket = nil; session = nil; retry = 0; opened = false
        connect()
    }

    private func connect() {
        Task { @MainActor [weak self] in
            guard let self, self.terminalView != nil else { return }
            self.status.transition(.fetchingToken)
            guard let token = await TerminalToken.fetch(from: self.box.terminalTokenURL, session: self.urlSession) else { self.status.failed(); return }
            self.open(token: token)
        }
    }

    private func open(token: String) {
        status.transition(.connecting)
        let socket = TtydWebSocket(url: box.terminalWebSocketURL(arg: arg),
                                   session: urlSession)
        let session = TtydSession(connection: socket, delegate: self)

        socket.onOpen = { [weak self] in
            Task { @MainActor in
                guard let self, let view = self.terminalView else { return }
                self.opened = true
                self.retry = 0
                self.status.transition(.connected)
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
        socket.onClose = { [weak self] error in
            Task { @MainActor in self?.closed(error) }
        }

        self.socket = socket
        self.session = session
        socket.connect()
    }

    private func closed(_ error: Error?) {
        if error != nil { status.failed(); socket = nil; session = nil; return }
        scheduleReconnect()
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
        status.reconnecting()
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
    var status: TerminalStatus? = nil

    final class Coordinator {
        let controller: TerminalController
        init(controller: TerminalController) { self.controller = controller }
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(controller: TerminalController(box: box, arg: arg, urlSession: urlSession, status: status ?? TerminalStatus()))
    }

    private func makeView(_ coordinator: Coordinator) -> TerminalView {
        let view = TerminalView(frame: .zero)
        #if os(iOS)
        view.nativeBackgroundColor = UIColor(red: 0.05, green: 0.05, blue: 0.05, alpha: 1)
        #else
        view.nativeBackgroundColor = NSColor(srgbRed: 5 / 255, green: 7 / 255, blue: 5 / 255, alpha: 1)
        view.nativeForegroundColor = NSColor(srgbRed: 214 / 255, green: 255 / 255, blue: 208 / 255, alpha: 1)
        view.caretColor = NSColor(srgbRed: 57 / 255, green: 211 / 255, blue: 83 / 255, alpha: 1)
        view.caretTextColor = view.nativeBackgroundColor
        view.selectedTextBackgroundColor = NSColor(srgbRed: 46 / 255, green: 74 / 255, blue: 46 / 255, alpha: 1)
        view.selectedTextForegroundColor = view.nativeForegroundColor
        view.font = NSFont.monospacedSystemFont(ofSize: 13, weight: .regular)
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
