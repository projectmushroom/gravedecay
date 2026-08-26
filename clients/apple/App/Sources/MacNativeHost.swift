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

    func update(snapshot: MacSnapshot?) { summary = Self.summaryData(snapshot: snapshot) }

    func enable() {
        guard listener == nil else { return }
        guard MacHostingPlan.preflight(legacyCompanionActive: Self.legacyCompanionActive()) == .attemptListener else { state = .existingCompanion; detail = "EXISTING COMPANION ACTIVE // USING 127.0.0.1:4712"; return }
        state = .starting; detail = "STARTING LOOPBACK SUMMARY…"
        do {
            let parameters = NWParameters.tcp
            parameters.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback), port: 4712)
            let listener = try NWListener(using: parameters)
            listener.stateUpdateHandler = { [weak self] status in DispatchQueue.main.async {
                guard let self else { return }
                switch status {
                case .ready: self.state = .hosted; self.detail = "HOSTING 127.0.0.1:4712 // APP PROCESS ONLY"
                case .failed: self.listener?.cancel(); self.listener = nil; self.state = .unavailable; self.detail = "PORT 4712 UNAVAILABLE // NOT STARTED"
                default: break
                }
            }}
            listener.newConnectionHandler = { [weak self] connection in Task { @MainActor in self?.accept(connection) } }
            self.listener = listener
            listener.start(queue: queue)
        } catch { state = .unavailable; detail = "PORT 4712 UNAVAILABLE // NOT STARTED" }
    }

    func disable() {
        listener?.cancel(); listener = nil; state = .off; detail = "OFF // NO LOCAL LISTENER"
    }

    var manualServeCommand: String { MacHostingPlan.manualServeCommand()! }

    private func accept(_ connection: NWConnection) {
        connection.start(queue: queue)
        queue.asyncAfter(deadline: .now() + 3) { connection.cancel() }
        receive(connection, request: Data())
    }

    private func receive(_ connection: NWConnection, request: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 4_096) { [weak self] data, _, _, _ in
            guard let self, let data, request.count + data.count <= 4_096 else { connection.cancel(); return }
            Task { @MainActor in
                var complete = request
                complete.append(data)
                guard let text = String(data: complete, encoding: .utf8) else { connection.cancel(); return }
                guard text.contains("\r\n\r\n") else { self.receive(connection, request: complete); return }
                let response = self.response(for: text)
                connection.send(content: response, completion: .contentProcessed { _ in connection.cancel() })
            }
        }
    }

    private func response(for request: String) -> Data {
        guard let end = request.range(of: "\r\n\r\n"), request.distance(from: request.startIndex, to: end.upperBound) <= 4_096 else { return Self.http(400, body: Data()) }
        let endOfLine = request.firstIndex(of: "\r") ?? request.endIndex
        let line = request[..<endOfLine].split(separator: " ")
        let lower = request.lowercased()
        guard line.count == 3, (line[0] == "GET" || line[0] == "HEAD"), line[2].hasPrefix("HTTP/"),
              !lower.contains("\r\ncontent-length:"), !lower.contains("\r\ntransfer-encoding:") else { return Self.http(405, body: Data()) }
        let body: Data
        switch String(line[1]) {
        case "/healthz": body = Data("{\"ok\":true}\n".utf8)
        case "/api/v1/summary": body = summary
        default: return Self.http(404, body: Data())
        }
        return Self.http(200, body: line[0] == "HEAD" ? Data() : body, length: body.count)
    }

    private static func http(_ status: Int, body: Data, length: Int? = nil) -> Data {
        let phrase = status == 200 ? "OK" : status == 404 ? "Not Found" : status == 405 ? "Method Not Allowed" : "Bad Request"
        return Data("HTTP/1.1 \(status) \(phrase)\r\nContent-Type: application/json\r\nCache-Control: no-store\r\nContent-Length: \(length ?? body.count)\r\nConnection: close\r\n\r\n".utf8) + body
    }

    private static func legacyCompanionActive() -> Bool {
        let process = Process(); process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = ["print", "gui/\(getuid())/io.gravedecay.dashboard"]
        process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
        guard (try? process.run()) != nil else { return false }
        process.waitUntilExit(); return process.terminationStatus == 0
    }

    private static func summaryData(snapshot: MacSnapshot?) -> Data {
        let value: [String: Any] = [
            "product": "gravedecay", "api_version": 1,
            "observed_at": ISO8601DateFormatter().string(from: .now),
            "node": ["host": Host.current().localizedName ?? "Mac", "platform": "macos", "mode": "companion", "uptime_s": ProcessInfo.processInfo.systemUptime],
            "resources": ["cpu_pct": snapshot?.cpuPercent as Any, "memory_pct": snapshot?.memoryPercent as Any, "disk_pct": snapshot?.diskPercent as Any, "cpu_temp_c": NSNull(), "gpu_temp_c": NSNull()],
            "activity": ["sessions_live": 0, "sessions_frozen": 0], "health": ["services_failed": 0, "containers_problem": 0],
            "links": ["dashboard": NSNull(), "t3": NSNull(), "terminal": NSNull(), "network": NSNull()]
        ]
        return (try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])) ?? Data("{}".utf8)
    }
}
#endif
