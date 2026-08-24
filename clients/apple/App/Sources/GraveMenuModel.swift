#if os(macOS)
import AppKit
import Foundation
import GravedecayKit

@MainActor
final class GraveMenuModel: ObservableObject {
    enum State: Equatable { case idle, scanning, missingTailscale, loggedOut, noAppliances, ready }
    struct Grave: Identifiable {
        let candidate: GraveCandidate
        var summary: GraveSummary?
        var reachable: Bool
        var id: String { candidate.id }
    }

    @Published private(set) var state: State = .idle
    @Published private(set) var graves: [Grave] = []
    @Published var selectedID: String?
    private var previous = [String: GraveSummary]()
    private var timer: Timer?

    var selected: Grave? { graves.first { $0.id == selectedID } }

    init() { start() }

    func start() {
        guard timer == nil else { return }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 45, repeats: true) { [weak self] _ in Task { @MainActor in self?.refresh() } }
    }
    func refresh() { Task { await scan() } }

    func scan() async {
        guard state != .scanning else { return }; state = .scanning
        guard let statusData = await Task.detached(priority: .utility, operation: Self.tailscaleStatus).value,
              let status = try? JSONSerialization.jsonObject(with: statusData) as? [String: Any] else {
            graves = []; state = .missingTailscale; return
        }
        guard !(status["BackendState"] as? String == "Stopped" || status["BackendState"] as? String == "NeedsLogin") else { graves = []; state = .loggedOut; return }
        let candidates = GraveDiscovery.candidates(statusData: statusData)
        guard !candidates.isEmpty else { graves = []; state = .noAppliances; return }
        var found: [Grave] = []
        for candidate in candidates {
            if let summary = await fetch(candidate) {
                previous[candidate.id] = summary; found.append(Grave(candidate: candidate, summary: summary, reachable: true))
            } else if let summary = previous[candidate.id] {
                found.append(Grave(candidate: candidate, summary: summary, reachable: false))
            }
        }
        graves = found
        if selectedID == nil || !found.contains(where: { $0.id == selectedID }) { selectedID = found.first?.id }
        state = found.isEmpty ? .noAppliances : .ready
    }

    nonisolated private static func tailscaleStatus() async -> Data? {
        let paths = ["/usr/local/bin/tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]
        guard let executable = paths.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else { return nil }
        let process = Process(); process.executableURL = URL(fileURLWithPath: executable); process.arguments = ["status", "--json"]
        process.environment = ProcessInfo.processInfo.environment.merging(["TAILSCALE_BE_CLI": "1"]) { _, new in new }
        let output = Pipe(); process.standardOutput = output; process.standardError = FileHandle.nullDevice
        return await withCheckedContinuation { continuation in
            let lock = NSLock(); var data = Data(); var finished = false; var exceeded = false
            func finish(_ result: Data?) {
                lock.lock(); defer { lock.unlock() }
                guard !finished else { return }; finished = true
                output.fileHandleForReading.readabilityHandler = nil
                continuation.resume(returning: result)
            }
            output.fileHandleForReading.readabilityHandler = { handle in
                let chunk = handle.availableData
                guard !chunk.isEmpty else { return }
                lock.lock(); data.append(chunk); let didExceed = data.count > 1_048_576; exceeded = didExceed; lock.unlock()
                if didExceed { process.terminate() }
            }
            process.terminationHandler = { _ in
                output.fileHandleForReading.readabilityHandler = nil
                let remainder = output.fileHandleForReading.readDataToEndOfFile()
                lock.lock(); data.append(remainder); let result = process.terminationStatus == 0 && !exceeded && data.count <= 1_048_576 ? data : nil; lock.unlock()
                finish(result)
            }
            do { try process.run() } catch { finish(nil); return }
            DispatchQueue.global().asyncAfter(deadline: .now() + 3) { if process.isRunning { process.terminate(); finish(nil) } }
        }
    }

    private func fetch(_ candidate: GraveCandidate) async -> GraveSummary? {
        guard let url = GravePresentation.link(host: candidate.dns, path: "/grave/api/v1/summary") else { return nil }
        var request = URLRequest(url: url); request.timeoutInterval = 3; request.setValue("application/json", forHTTPHeaderField: "Accept")
        do {
            let (bytes, response) = try await Self.noRedirectSession.bytes(for: request)
            guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode),
                  (http.value(forHTTPHeaderField: "Content-Length").flatMap(Int.init) ?? 0) <= 65_536 else { return nil }
            var data = Data(); var iterator = bytes.makeAsyncIterator()
            while let byte = try await iterator.next() { data.append(byte); if data.count > 65_536 { return nil } }
            return GraveSummary.decode(data)
        } catch { return nil }
    }

    func open(_ path: String?) { guard let grave = selected, let url = GravePresentation.link(host: grave.candidate.dns, path: path) else { return }; NSWorkspace.shared.open(url) }

    private static let noRedirectSession = URLSession(configuration: .ephemeral, delegate: NoRedirectDelegate(), delegateQueue: nil)
}

private final class NoRedirectDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) { completionHandler(nil) }
}
#endif
