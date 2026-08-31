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
        let probe = await Task.detached(priority: .utility) { Self.tailscaleStatus() }.value
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

    nonisolated private static func tailscaleStatus() -> (executableFound: Bool, data: Data?) {
        let paths = ["/usr/local/bin/tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]
        guard let executable = paths.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else { return (false, nil) }
        return (true, MacCollector.command(executable, ["status", "--json"], environment: ["TAILSCALE_BE_CLI": "1"], deadline: Date().addingTimeInterval(3)).map { Data($0.utf8) })
    }

    var canOpenTailscale: Bool { FileManager.default.fileExists(atPath: "/Applications/Tailscale.app") }
    func getTailscale() { if let url = URL(string: "https://tailscale.com/download/mac") { NSWorkspace.shared.open(url) } }
    func openTailscale() { guard canOpenTailscale else { return }; NSWorkspace.shared.open(URL(fileURLWithPath: "/Applications/Tailscale.app")) }

    private func fetch(_ candidate: GraveCandidate) async -> GraveSummary? {
        guard let url = GravePresentation.link(host: candidate.dns, path: "/grave/api/v1/summary") else { return nil }
        var request = URLRequest(url: url); request.timeoutInterval = 3; request.setValue("application/json", forHTTPHeaderField: "Accept")
        guard case let (data, response)? = try? await URLSession.shared.data(for: request), (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return GraveSummary.decode(data)
    }

    func open(_ path: String?) { guard let grave = selected, let url = GravePresentation.link(host: grave.candidate.dns, path: path) else { return }; NSWorkspace.shared.open(url) }

    func select(_ grave: Grave) { selectedID = grave.id }
}
#endif
