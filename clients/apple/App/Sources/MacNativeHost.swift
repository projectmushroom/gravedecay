#if os(macOS)
import Foundation
import Network
import GravedecayKit

/// Optional, in-process publisher for the Mac companion. It deliberately has
/// no Serve control: publishing a tailnet path remains an explicit manual
/// action because the CLI's global configuration cannot prove route ownership.
@MainActor
final class MacNativeHost: ObservableObject {
    enum State: Equatable { case off, starting, hosted, existingCompanion, unavailable }
    @Published private(set) var state: State = .off
    @Published private(set) var detail = "OFF // NO LOCAL LISTENER"
    private var listener: NWListener?
    private var summary = Data()
    private let queue = DispatchQueue(label: "com.projectmushroom.gravedecay.host")
    private let defaults: UserDefaults
    private var restoreAttempted = false
    private var requested: Bool
    var hostRequested: Bool { requested }

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        requested = defaults.bool(forKey: "nativeHostEnabled")
    }

    func update(snapshot: MacSnapshot?) { summary = Self.summaryData(snapshot: snapshot) }

    func restoreIfRequested() {
        guard !restoreAttempted, MacHostingPlan.restore(nativeHostEnabled: requested) == .attempt else { return }
        restoreAttempted = true; start()
    }

    func enable() { start() }

    private func start() {
        guard listener == nil else { return }
        guard MacHostingPlan.preflight(legacyCompanionActive: Self.legacyCompanionActive()) == .attemptListener else { state = .existingCompanion; detail = requested ? "REQUESTED BUT BLOCKED // LEGACY COMPANION OWNS 4712" : "EXISTING COMPANION ACTIVE // NATIVE HOST NOT STARTED"; return }
        state = .starting; detail = "STARTING LOOPBACK SUMMARY…"
        do {
            let parameters = NWParameters.tcp
            parameters.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback), port: 4712)
            let listener = try NWListener(using: parameters)
            listener.stateUpdateHandler = { [weak self] status in DispatchQueue.main.async {
                guard let self else { return }
                switch status {
                case .ready: self.requested = true; self.defaults.set(true, forKey: "nativeHostEnabled"); self.state = .hosted; self.detail = "HOSTING 127.0.0.1:4712 // APP PROCESS ONLY"
                case .failed: self.listener?.cancel(); self.listener = nil; self.state = .unavailable; self.detail = self.requested ? "REQUESTED BUT BLOCKED // PORT 4712 UNAVAILABLE" : "PORT 4712 UNAVAILABLE // NOT STARTED"
                default: break
                }
            }}
            listener.newConnectionHandler = { [weak self] connection in Task { @MainActor in self?.accept(connection) } }
            self.listener = listener
            listener.start(queue: queue)
        } catch { state = .unavailable; detail = requested ? "REQUESTED BUT BLOCKED // PORT 4712 UNAVAILABLE" : "PORT 4712 UNAVAILABLE // NOT STARTED" }
    }

    func disable() {
        listener?.cancel(); listener = nil; requested = false; defaults.set(false, forKey: "nativeHostEnabled"); state = .off; detail = "OFF // NO LOCAL LISTENER"
    }

    var manualServeCommand: String { MacHostingPlan.manualServeCommand()! }

    private func accept(_ connection: NWConnection) {
        connection.start(queue: queue)
        queue.asyncAfter(deadline: .now() + 3) { connection.cancel() }
        receive(connection, request: Data())
    }

    private func receive(_ connection: NWConnection, request: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 4_096) { [weak self] data, _, _, _ in
            guard let self, let data, request.count + data.count <= MacPublisherHTTP.maxRequestBytes else { connection.cancel(); return }
            Task { @MainActor in
                var complete = request
                complete.append(data)
                guard let text = String(data: complete, encoding: .utf8) else { connection.cancel(); return }
                guard text.contains("\r\n\r\n") else { self.receive(connection, request: complete); return }
                let response = MacPublisherHTTP.response(request: complete, summary: self.summary)
                connection.send(content: response, completion: .contentProcessed { _ in connection.cancel() })
            }
        }
    }

    private static func legacyCompanionActive() -> Bool {
        let process = Process(); process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = ["print", "gui/\(getuid())/io.gravedecay.dashboard"]
        process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
        guard (try? process.run()) != nil else { return false }
        process.waitUntilExit(); return process.terminationStatus == 0
    }

    private static func summaryData(snapshot: MacSnapshot?) -> Data {
        MacPublisherSummary.data(host: Host.current().localizedName ?? "Mac", uptime: ProcessInfo.processInfo.systemUptime, cpu: snapshot?.cpuPercent, memory: snapshot?.memoryPercent, disk: snapshot?.diskPercent)
    }
}
#endif
