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

    init() { Task { [weak self] in await self?.scan() } }

    func start() {
        guard timer == nil else { return }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 45, repeats: true) { [weak self] _ in Task { @MainActor in self?.refresh() } }
    }
    func refresh() { Task { await scan() } }

    func scan() async {
        guard state != .scanning else { return }; state = .scanning
        guard let status = await tailscaleStatus() else { state = .missingTailscale; return }
        guard !(status["BackendState"] as? String == "Stopped" || status["BackendState"] as? String == "NeedsLogin") else { state = .loggedOut; return }
        let candidates = GraveDiscovery.candidates(statusData: (try? JSONSerialization.data(withJSONObject: status)) ?? Data())
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

    private func tailscaleStatus() async -> [String: Any]? {
        let paths = ["/usr/local/bin/tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]
        guard let executable = paths.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else { return nil }
        let process = Process(); process.executableURL = URL(fileURLWithPath: executable); process.arguments = ["status", "--json"]
        process.environment = ProcessInfo.processInfo.environment.merging(["TAILSCALE_BE_CLI": "1"]) { _, new in new }
        let output = Pipe(); process.standardOutput = output; process.standardError = Pipe()
        return await withCheckedContinuation { continuation in
            process.terminationHandler = { _ in
                let data = output.fileHandleForReading.readDataToEndOfFile()
                continuation.resume(returning: (try? JSONSerialization.jsonObject(with: data)) as? [String: Any])
            }
            do { try process.run() } catch { continuation.resume(returning: nil) }
        }
    }

    private func fetch(_ candidate: GraveCandidate) async -> GraveSummary? {
        guard let url = GravePresentation.link(host: candidate.dns, path: "/grave/api/v1/summary") else { return nil }
        var request = URLRequest(url: url); request.timeoutInterval = 3; request.setValue("application/json", forHTTPHeaderField: "Accept")
        do {
            let (bytes, response) = try await URLSession.shared.bytes(for: request)
            guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode),
                  (http.value(forHTTPHeaderField: "Content-Length").flatMap(Int.init) ?? 0) <= 65_536 else { return nil }
            var data = Data(); var iterator = bytes.makeAsyncIterator()
            while let byte = try await iterator.next() { data.append(byte); if data.count > 65_536 { return nil } }
            return GraveSummary.decode(data)
        } catch { return nil }
    }

    func open(_ path: String?) { guard let grave = selected, let url = GravePresentation.link(host: grave.candidate.dns, path: path) else { return }; NSWorkspace.shared.open(url) }
    func showMainWindow() { NSApp.activate(ignoringOtherApps: true); NSApp.windows.first(where: { $0.canBecomeMain })?.makeKeyAndOrderFront(nil) }
}
#endif
