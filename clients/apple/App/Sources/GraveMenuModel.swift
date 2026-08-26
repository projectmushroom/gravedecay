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
    @Published private(set) var tailscaleUnavailable = false
    @Published var selectedID: String? { didSet { UserDefaults.standard.set(selectedID, forKey: "graveSelectedTarget") } }
    private var previous = [String: GraveSummary]()
    private var timer: Timer?

    var selected: Grave? { graves.first { $0.id == selectedID } }

    init() {
        selectedID = UserDefaults.standard.string(forKey: "graveSelectedTarget")
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 45, repeats: true) { [weak self] _ in Task { @MainActor in self?.refresh() } }
    }
    func refresh() { Task { await scan() } }

    func scan() async {
        guard state != .scanning else { return }; state = .scanning
        tailscaleUnavailable = false
        let probe = await Task.detached(priority: .utility, operation: Self.tailscaleStatus).value
        let statusData = probe.data
        switch GraveDiscovery.tailscaleState(executableFound: probe.executableFound, statusData: statusData) {
        case .missing: graves = []; selectedID = nil; state = .missingTailscale; return
        case .unavailable: graves = []; selectedID = nil; tailscaleUnavailable = true; state = .noAppliances; return
        case .loggedOut: graves = []; selectedID = nil; state = .loggedOut; return
        case .running: break
        }
        guard let statusData else { return }
        let candidates = GraveDiscovery.candidates(statusData: statusData)
        guard !candidates.isEmpty else { graves = []; selectedID = nil; state = .noAppliances; return }
        var found: [Grave] = []
        for candidate in candidates {
            if let summary = await fetch(candidate) {
                previous[candidate.id] = summary; found.append(Grave(candidate: candidate, summary: summary, reachable: true))
            } else if let summary = previous[candidate.id] {
                found.append(Grave(candidate: candidate, summary: summary, reachable: false))
            }
        }
        graves = found
        selectedID = GraveDiscovery.selectedID(previousID: selectedID, candidates: found.map(\.candidate))
        state = found.isEmpty ? .noAppliances : .ready
    }

    nonisolated private static func tailscaleStatus() async -> (executableFound: Bool, data: Data?) {
        let paths = ["/usr/local/bin/tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]
        guard let executable = paths.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else { return (false, nil) }
        let process = Process(); process.executableURL = URL(fileURLWithPath: executable); process.arguments = ["status", "--json"]
        process.environment = ProcessInfo.processInfo.environment.merging(["TAILSCALE_BE_CLI": "1"]) { _, new in new }
        let output = Pipe(); process.standardOutput = output; process.standardError = FileHandle.nullDevice
        return await withCheckedContinuation { continuation in
            let lock = NSLock(); var data = Data(); var finished = false; var exceeded = false
            func finish(_ result: Data?) {
                lock.lock(); defer { lock.unlock() }
                guard !finished else { return }; finished = true
                output.fileHandleForReading.readabilityHandler = nil
                continuation.resume(returning: (true, result))
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

    var canOpenTailscale: Bool { FileManager.default.fileExists(atPath: "/Applications/Tailscale.app") }
    func getTailscale() { if let url = URL(string: "https://tailscale.com/download/mac") { NSWorkspace.shared.open(url) } }
    func openTailscale() { guard canOpenTailscale else { return }; NSWorkspace.shared.open(URL(fileURLWithPath: "/Applications/Tailscale.app")) }

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

    func select(_ grave: Grave) { selectedID = grave.id }

    private static let noRedirectSession: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 3
        configuration.timeoutIntervalForResource = 3
        return URLSession(configuration: configuration, delegate: NoRedirectDelegate(), delegateQueue: nil)
    }()
}

private final class NoRedirectDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) { completionHandler(nil) }
}
#endif
