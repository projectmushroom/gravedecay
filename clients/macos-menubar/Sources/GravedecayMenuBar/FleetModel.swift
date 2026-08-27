import AppKit
import Combine
import Foundation
import GravedecayKit

struct FleetBox: Identifiable, Equatable, Sendable {
    let candidate: GraveCandidate
    let summary: GraveSummary
    var id: String { candidate.id }
}

@MainActor
final class FleetModel: ObservableObject {
    typealias StatusLoader = () async -> (executableFound: Bool, data: Data?)
    typealias SummaryLoader = (GraveCandidate) async -> GraveSummary?

    enum State: Equatable { case idle, scanning, missingTailscale, loggedOut, noGraves, ready }
    enum MenuState {
        case neutral(String), clear, attention

        var title: String {
            switch self {
            case .neutral(let detail): return "Gravedecay · \(detail)"
            case .clear: return "Gravedecay · All clear"
            case .attention: return "Gravedecay · Attention"
            }
        }

        var symbol: String {
            switch self {
            case .neutral: return "circle.dashed"
            case .clear: return "checkmark.circle.fill"
            case .attention: return "exclamationmark.triangle.fill"
            }
        }
    }

    @Published private(set) var state: State = .idle
    @Published private(set) var boxes: [FleetBox] = []
    private let statusLoader: StatusLoader
    private let summaryLoader: SummaryLoader
    private var timer: Timer?

    init(start: Bool = true, statusLoader: @escaping StatusLoader = FleetModel.tailscaleStatus, summaryLoader: @escaping SummaryLoader = FleetModel.fetch) {
        self.statusLoader = statusLoader
        self.summaryLoader = summaryLoader
        if start {
            refresh()
            timer = Timer.scheduledTimer(withTimeInterval: 45, repeats: true) { [weak self] _ in Task { @MainActor in self?.refresh() } }
        }
    }

    func refresh() { Task { await scan() } }

    func scan() async {
        guard state != .scanning else { return }
        state = .scanning
        let status = await statusLoader()
        switch GraveDiscovery.tailscaleState(executableFound: status.executableFound, statusData: status.data) {
        case .missing: boxes = []; state = .missingTailscale; return
        case .loggedOut: boxes = []; state = .loggedOut; return
        case .unavailable: boxes = []; state = .noGraves; return
        case .running: break
        }
        guard let data = status.data else { boxes = []; state = .noGraves; return }
        let candidates = GraveDiscovery.candidates(statusData: data)
        let loader = summaryLoader
        let found = await withTaskGroup(of: FleetBox?.self, returning: [FleetBox].self) { group in
            for candidate in candidates {
                group.addTask { await loader(candidate).map { FleetBox(candidate: candidate, summary: $0) } }
            }
            return await group.reduce(into: []) { if let box = $1 { $0.append(box) } }
        }.sorted {
            let order = $0.summary.node.host.localizedCaseInsensitiveCompare($1.summary.node.host)
            return order == .orderedAscending || (order == .orderedSame && $0.id < $1.id)
        }
        boxes = found
        state = found.isEmpty ? .noGraves : .ready
    }

    var needsAttention: Bool { boxes.contains { $0.summary.problems > 0 } }
    var menuState: MenuState {
        guard !boxes.isEmpty else {
            switch state {
            case .scanning: return .neutral("Discovering")
            case .missingTailscale: return .neutral("Tailscale not installed")
            case .loggedOut: return .neutral("Tailscale sign-in required")
            default: return .neutral("No reachable graves")
            }
        }
        return needsAttention ? .attention : .clear
    }

    func dashboardURL(for box: FleetBox) -> URL? {
        GravePresentation.link(host: box.candidate.dns, path: box.summary.links.dashboard)
    }

    func openDashboard(_ box: FleetBox) {
        guard let url = dashboardURL(for: box) else { return }
        NSWorkspace.shared.open(url)
    }

    nonisolated private static func tailscaleStatus() async -> (executableFound: Bool, data: Data?) {
        await Task.detached(priority: .utility) {
            let paths = ["/usr/local/bin/tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]
            guard let executable = paths.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else { return (false, nil) }
            let process = Process(); process.executableURL = URL(fileURLWithPath: executable); process.arguments = ["status", "--json"]
            process.environment = ProcessInfo.processInfo.environment.merging(["TAILSCALE_BE_CLI": "1"]) { _, new in new }
            let output = Pipe(); process.standardOutput = output; process.standardError = FileHandle.nullDevice
            guard (try? process.run()) != nil else { return (true, nil) }
            let data = output.fileHandleForReading.readDataToEndOfFile(); process.waitUntilExit()
            return (true, process.terminationStatus == 0 && data.count <= 1_048_576 ? data : nil)
        }.value
    }

    nonisolated private static func fetch(_ candidate: GraveCandidate) async -> GraveSummary? {
        guard let url = GravePresentation.link(host: candidate.dns, path: "/grave/api/v1/summary") else { return nil }
        var request = URLRequest(url: url); request.timeoutInterval = 3; request.setValue("application/json", forHTTPHeaderField: "Accept")
        do {
            let (bytes, response) = try await noRedirectSession.bytes(for: request)
            guard let response = response as? HTTPURLResponse, (200..<300).contains(response.statusCode),
                  (response.value(forHTTPHeaderField: "Content-Length").flatMap(Int.init) ?? 0) <= 65_536 else { return nil }
            var data = Data(); var iterator = bytes.makeAsyncIterator()
            while let byte = try await iterator.next() { data.append(byte); if data.count > 65_536 { return nil } }
            return GraveSummary.decode(data)
        } catch { return nil }
    }

    nonisolated private static let noRedirectSession: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 3; configuration.timeoutIntervalForResource = 3
        return URLSession(configuration: configuration, delegate: NoRedirectDelegate(), delegateQueue: nil)
    }()
}

private final class NoRedirectDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) { completionHandler(nil) }
}
