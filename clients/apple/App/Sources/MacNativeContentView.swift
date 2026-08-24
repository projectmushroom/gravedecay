#if os(macOS)
import AppKit
import SwiftUI
import Security
import ServiceManagement
import Darwin
import GravedecayKit

/// The Mac product deliberately renders its own surfaces.  The only web
/// destinations are explicit browser hand-offs to T3 and legacy dashboards.
struct MacNativeContentView: View {
    @ObservedObject var graves: GraveMenuModel
    @ObservedObject var model: MacDashboardModel
    @State private var selection: Section = .system

    enum Section: Hashable { case system, work, network, appliances, terminal, settings }

    var body: some View {
        NavigationSplitView {
            List(selection: $selection) {
                Label("This Mac", systemImage: "desktopcomputer").tag(Section.system)
                Label("Work", systemImage: "folder").tag(Section.work)
                Label("Network", systemImage: "network").tag(Section.network)
                Label("Appliances", systemImage: "server.rack").tag(Section.appliances)
                Label("Terminal", systemImage: "terminal").tag(Section.terminal)
                Divider()
                Label("Settings", systemImage: "gearshape").tag(Section.settings)
            }
            .navigationTitle("Gravedecay")
            .frame(minWidth: 180)
        } detail: {
            Group {
                switch selection {
                case .system: SystemView(snapshot: model.snapshot)
                case .work: WorkView(model: model)
                case .network: NetworkView(model: model)
                case .appliances: AppliancesView(graves: graves)
                case .terminal: TerminalDestination(graves: graves)
                case .settings: MacSettingsView(model: model)
                }
            }
            .toolbar {
                Button("Refresh", systemImage: "arrow.clockwise") { model.refresh(); graves.refresh() }
            }
        }
        .task { await model.refreshAsync(); graves.refresh() }
        .frame(minWidth: 760, minHeight: 500)
    }
}

private struct SystemView: View {
    let snapshot: MacSnapshot?
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text("This Mac").font(.largeTitle.bold())
                if let snapshot {
                    HStack(spacing: 14) {
                        MetricCard(title: "CPU", value: snapshot.cpu, icon: "cpu")
                        MetricCard(title: "Memory", value: snapshot.memory, icon: "memorychip")
                        MetricCard(title: "Disk", value: snapshot.disk, icon: "internaldrive")
                    }
                    GroupBox("System") {
                        LabeledContent("Model", value: snapshot.model)
                        LabeledContent("macOS", value: snapshot.os)
                        LabeledContent("Uptime", value: snapshot.uptime)
                        LabeledContent("Battery", value: snapshot.battery)
                        LabeledContent("Thermal", value: snapshot.thermal)
                        LabeledContent("Swap", value: snapshot.swap)
                    }
                } else { ProgressView("Reading native system status…") }
            }.padding()
        }.navigationTitle("This Mac")
    }
}

private struct MetricCard: View {
    let title: String; let value: String; let icon: String
    var body: some View {
        VStack(alignment: .leading, spacing: 8) { Label(title, systemImage: icon).foregroundStyle(.secondary); Text(value).font(.title2.bold()) }
            .frame(maxWidth: .infinity, alignment: .leading).padding().background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
    }
}

private struct WorkView: View {
    @ObservedObject var model: MacDashboardModel
    var body: some View {
        List {
            Section("Repositories") {
                if model.repositories.isEmpty { ContentUnavailableView("No Git repositories", systemImage: "folder", description: Text("Set a work folder in Settings.")) }
                ForEach(model.repositories) { repo in
                    VStack(alignment: .leading) {
                        HStack { Text(repo.name).font(.headline); Spacer(); Text(repo.branch).foregroundStyle(.secondary) }
                        Text(repo.detail).font(.subheadline).foregroundStyle(repo.dirty ? .orange : .secondary)
                        if let github = repo.github { Text(github).font(.caption).foregroundStyle(.secondary) }
                        if let url = repo.githubURL { Link("Open GitHub", destination: url).font(.caption) }
                    }.padding(.vertical, 3)
                }
            }
            Section("Integrations") {
                Label(model.githubStatus, systemImage: "chevron.left.forwardslash.chevron.right")
                Label(model.linearStatus, systemImage: "line.3.horizontal")
                if !model.linearIssues.isEmpty { ForEach(model.linearIssues, id: \.self) { Text($0) } }
            }
        }.navigationTitle("Work")
    }
}

private struct NetworkView: View {
    @ObservedObject var model: MacDashboardModel
    var body: some View {
        Form {
            Section("Tailscale") { LabeledContent("Status", value: model.tailnetStatus); LabeledContent("Device", value: model.tailnetName) }
            Section("Active interface") { Text(model.networkActivity).font(.footnote) }
            Section { Text("Gravedecay only reads status. It never changes Tailscale login, preferences, or Serve configuration.").font(.footnote).foregroundStyle(.secondary) }
        }.formStyle(.grouped).navigationTitle("Network")
    }
}

private struct AppliancesView: View {
    @ObservedObject var graves: GraveMenuModel
    var body: some View {
        List {
            if graves.graves.isEmpty { ContentUnavailableView("No appliances", systemImage: "server.rack", description: Text("Sign into the Tailscale app, then refresh.")) }
            ForEach(graves.graves) { grave in
                VStack(alignment: .leading, spacing: 4) {
                    HStack { Label(grave.candidate.name, systemImage: icon(for: GravePresentation.condition(summary: grave.summary, reachable: grave.reachable))); Spacer(); Text(grave.reachable ? "Online" : "Unreachable").foregroundStyle(grave.reachable ? .green : .secondary) }
                    if let summary = grave.summary { Text("\(summary.node.platform) · \(summary.problems) problems · CPU \(GravePresentation.percent(summary.resources.cpu_pct))").foregroundStyle(.secondary) }
                    HStack { BrowserButton(title: "T3", host: grave.candidate.dns, path: "/"); BrowserButton(title: "Dashboard", host: grave.candidate.dns, path: grave.summary?.links.dashboard ?? "/grave/") }
                }.padding(.vertical, 4).contentShape(Rectangle()).onTapGesture { graves.selectedID = grave.id }
            }
        }.navigationTitle("Appliances")
    }
    private func icon(for condition: GravePresentation.Condition) -> String {
        switch condition {
        case .unreachable: return "wifi.slash"
        case .warning: return "exclamationmark.triangle.fill"
        case .active: return "bolt.circle.fill"
        case .frozen: return "snowflake"
        case .healthy: return "checkmark.circle.fill"
        }
    }
}

private struct TerminalDestination: View {
    @ObservedObject var graves: GraveMenuModel
    var body: some View {
        if let grave = graves.selected, let box = BoxConfig(input: grave.candidate.dns) { TerminalPane(box: box, urlSession: .shared).navigationTitle("Terminal — \(grave.candidate.name)") }
        else { ContentUnavailableView("Choose an appliance", systemImage: "terminal", description: Text("Use the Appliances view after Tailscale discovery completes.")) }
    }
}

struct MacSettingsView: View {
    @ObservedObject var model: MacDashboardModel
    @State private var root = ""; @State private var linearKey = ""
    var body: some View {
        Form {
            Section("Work") { TextField("Repository folder", text: $root); Button("Save work folder") { model.setWorkRoot(root) } }
            Section("Linear") { SecureField("API key", text: $linearKey); Button("Save Linear key") { model.saveLinearKey(linearKey); linearKey = "" }; Button("Remove Linear key", role: .destructive) { model.removeLinearKey() }; Text(model.keychainStatus).font(.footnote).foregroundStyle(.secondary) }
            Section("Launch at Login") { Toggle("Launch Gravedecay at login", isOn: $model.launchAtLogin).onChange(of: model.launchAtLogin) { _, value in model.setLaunchAtLogin(value) }; if let error = model.launchAtLoginError { Text(error).font(.footnote).foregroundStyle(.red) } }
            Section("About") { Text("Native macOS 15+ UI. T3 and legacy web dashboards open in your default browser.").font(.footnote) }
        }.formStyle(.grouped).navigationTitle("Settings").onAppear { root = model.workRoot }
    }
}

private struct BrowserButton: View {
    let title: String; let host: String; let path: String
    var body: some View { Button(title) { if let url = GravePresentation.link(host: host, path: path) { NSWorkspace.shared.open(url) } } }
}

struct MacRepository: Identifiable { let path, name, branch, detail: String; let dirty: Bool; let github: String?; let githubURL: URL?; var id: String { path } }
struct MacSnapshot { let model, os, cpu, memory, disk, uptime, battery, thermal, swap: String }

@MainActor
final class MacDashboardModel: ObservableObject {
    @Published private(set) var snapshot: MacSnapshot?
    @Published private(set) var repositories: [MacRepository] = []
    @Published private(set) var tailnetStatus = "Checking…"
    @Published private(set) var tailnetName = "—"
    @Published private(set) var githubStatus = "Checking GitHub CLI…"
    @Published private(set) var linearStatus = "Linear not configured"
    @Published private(set) var linearIssues: [String] = []
    @Published private(set) var networkActivity = "Checking…"
    @Published private(set) var workRoot: String
    @Published private(set) var keychainStatus = "Stored in this Mac’s Keychain. Assigned issues are read-only."
    @Published var launchAtLogin = SMAppService.mainApp.status == .enabled
    @Published private(set) var launchAtLoginError: String?
    private let defaults = UserDefaults.standard
    init() { workRoot = UserDefaults.standard.string(forKey: "macWorkRoot") ?? (FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Sites").path) }
    func refresh() { Task { await refreshAsync() } }
    func setWorkRoot(_ value: String) { let url = URL(fileURLWithPath: (value as NSString).expandingTildeInPath); guard FileManager.default.fileExists(atPath: url.path) else { return }; workRoot = url.path; defaults.set(workRoot, forKey: "macWorkRoot"); refresh() }
    func saveLinearKey(_ key: String) { guard !key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }; keychainStatus = Keychain.set(key, account: "linear-api-key") ? "Stored in this Mac’s Keychain." : "Could not save the Linear key to Keychain."; refresh() }
    func removeLinearKey() { keychainStatus = Keychain.remove(account: "linear-api-key") ? "Linear key removed." : "Could not remove the Linear key from Keychain."; refresh() }
    func setLaunchAtLogin(_ enabled: Bool) {
        do { if enabled { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }; launchAtLogin = SMAppService.mainApp.status == .enabled; launchAtLoginError = nil }
        catch { launchAtLogin = SMAppService.mainApp.status == .enabled; launchAtLoginError = "Launch at Login: \(error.localizedDescription)" }
    }
    func refreshAsync() async {
        let root = workRoot; let key = Keychain.value(account: "linear-api-key")
        let result = await Task.detached(priority: .utility) { MacCollector.collect(root: root, linearKey: key) }.value
        snapshot = result.snapshot; repositories = result.repositories; tailnetStatus = result.tailnetStatus; tailnetName = result.tailnetName; githubStatus = result.githubStatus; linearStatus = result.linearStatus; linearIssues = result.linearIssues; networkActivity = result.networkActivity
    }
}

private struct CollectionResult { let snapshot: MacSnapshot; let repositories: [MacRepository]; let tailnetStatus, tailnetName, githubStatus, linearStatus, networkActivity: String; let linearIssues: [String] }
private enum MacCollector {
    static func collect(root: String, linearKey: String?) -> CollectionResult {
        let os = ProcessInfo.processInfo.operatingSystemVersionString.replacingOccurrences(of: "Version ", with: "")
        let disk = (try? URL(fileURLWithPath: NSHomeDirectory()).resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey, .volumeTotalCapacityKey]))
        let total = Int64(disk?.volumeTotalCapacity ?? 0), available = disk?.volumeAvailableCapacityForImportantUsage ?? 0
        let used: String
        if total > 0 {
            let percent = Int((Double(total - available) / Double(total)) * 100)
            used = "\(percent)% used"
        } else { used = "—" }
        let memory = command("/usr/bin/memory_pressure", ["-Q"])?.split(separator: "\n").prefix(2).joined(separator: " · ") ?? "Memory pressure unavailable"
        let cpu = cpuUsage(command("/usr/bin/top", ["-l", "1", "-n", "0"])) ?? "CPU activity unavailable"
        let uptime = GravePresentation.uptime(ProcessInfo.processInfo.systemUptime)
        let battery = command("/usr/bin/pmset", ["-g", "batt"]).map { $0.contains("AC Power") ? "AC Power" : ($0.split(whereSeparator: { $0 == ";" }).first.map(String.init) ?? "Battery") } ?? "Not available"
        let thermal = command("/usr/bin/pmset", ["-g", "therm"]).map { $0.localizedCaseInsensitiveContains("CPU_Speed_Limit") ? $0.replacingOccurrences(of: "\n", with: " ") : "No thermal limit reported" } ?? "Thermal status unavailable"
        let swap = command("/usr/sbin/sysctl", ["-n", "vm.swapusage"]) ?? "Swap unavailable"
        let snapshot = MacSnapshot(model: command("/usr/sbin/sysctl", ["-n", "hw.model"]) ?? "Mac", os: os, cpu: cpu, memory: memory, disk: used, uptime: uptime, battery: battery, thermal: thermal, swap: swap)
        let status = command("/usr/local/bin/tailscale", ["status", "--json"], environment: ["TAILSCALE_BE_CLI": "1"]) ?? command("/Applications/Tailscale.app/Contents/MacOS/Tailscale", ["status", "--json"], environment: ["TAILSCALE_BE_CLI": "1"])
        let tailnet = status.flatMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
        let backend = tailnet?["BackendState"] as? String ?? "Not installed or signed out"
        let name = ((tailnet?["Self"] as? [String: Any])?["DNSName"] as? String) ?? "—"
        let gh = command("/opt/homebrew/bin/gh", ["auth", "status"]) ?? command("/usr/local/bin/gh", ["auth", "status"])
        let github = gh == nil ? "GitHub CLI not installed or not signed in" : "GitHub CLI authenticated (read-only PR/CI status)"
        let repos = repositories(at: root, ghAvailable: gh != nil)
        let linear = linearIssues(key: linearKey)
        let network = command("/usr/sbin/scutil", ["--nwi"])?.split(separator: "\n").first(where: { $0.contains("Network interfaces") || $0.contains("IPv4") }).map(String.init) ?? "Active interface unavailable"
        return CollectionResult(snapshot: snapshot, repositories: repos, tailnetStatus: backend, tailnetName: name, githubStatus: github, linearStatus: linear.0, networkActivity: network, linearIssues: linear.1)
    }
    static func cpuUsage(_ top: String?) -> String? {
        guard let top, let range = top.range(of: "CPU usage: [0-9.]+% user, [0-9.]+% sys", options: .regularExpression) else { return nil }
        return String(top[range]).replacingOccurrences(of: "CPU usage: ", with: "")
    }
    static func repositories(at root: String, ghAvailable: Bool) -> [MacRepository] {
        guard let enumerator = FileManager.default.enumerator(at: URL(fileURLWithPath: root), includingPropertiesForKeys: [.isDirectoryKey], options: [.skipsPackageDescendants]) else { return [] }
        var found = [String](); let rootDepth = URL(fileURLWithPath: root).pathComponents.count; let deadline = Date().addingTimeInterval(2); var entries = 0
        while let url = enumerator.nextObject() as? URL, found.count < 24, entries < 2_000, Date() < deadline {
            entries += 1
            let name = url.lastPathComponent
            if url.pathComponents.count - rootDepth > 5 { enumerator.skipDescendants(); continue }
            if name == ".git" { found.append(url.deletingLastPathComponent().path); enumerator.skipDescendants() }
            else if name.hasPrefix(".") { enumerator.skipDescendants() }
        }
        return found.sorted().enumerated().compactMap { index, path in
            let branch = command("/usr/bin/git", ["-C", path, "branch", "--show-current"]) ?? "detached"
            let status = command("/usr/bin/git", ["-C", path, "status", "--porcelain"]) ?? ""
            let dirty = !status.isEmpty; let detail = dirty ? "\(WorkStatus.changedFileCount(status)) changed file(s)" : "Clean"
            let remote = command("/usr/bin/git", ["-C", path, "remote", "get-url", "origin"])
            let github = githubSummary(remote, enabled: ghAvailable && index < 8)
            return MacRepository(path: path, name: URL(fileURLWithPath: path).lastPathComponent, branch: branch, detail: detail, dirty: dirty, github: github, githubURL: GitHubRemote.url(remote))
        }
    }
    static func githubSummary(_ remote: String?, enabled: Bool) -> String? {
        guard let remote, remote.contains("github.com") else { return nil }
        guard enabled else { return "GitHub remote" }
        let cleaned = remote.replacingOccurrences(of: ".git", with: "")
        guard let slash = cleaned.range(of: "github.com[:/]", options: .regularExpression) else { return "GitHub remote" }
        let repo = String(cleaned[slash.upperBound...])
        let output = command("/opt/homebrew/bin/gh", ["pr", "list", "--repo", repo, "--limit", "3", "--json", "number,statusCheckRollup"]) ?? command("/usr/local/bin/gh", ["pr", "list", "--repo", repo, "--limit", "3", "--json", "number,statusCheckRollup"])
        guard let output, let prs = try? JSONSerialization.jsonObject(with: Data(output.utf8)) as? [[String: Any]] else { return "GitHub unavailable" }
        let failing = prs.contains { (($0["statusCheckRollup"] as? [[String: Any]]) ?? []).contains { ["FAILURE", "ERROR"].contains($0["conclusion"] as? String ?? "") } }
        return "GitHub: \(prs.count) open PR(s)\(failing ? ", CI failing" : "")"
    }
    static func linearIssues(key: String?) -> (String, [String]) {
        guard let key, !key.isEmpty else { return ("Linear not configured — add a key in Settings", []) }
        // This first native release preserves the read-only contract; the query stays bounded.
        let body = #"{"query":"query { viewer { assignedIssues(first: 10) { nodes { identifier title } } } }"}"#
        guard let url = URL(string: "https://api.linear.app/graphql"), var request = Optional(URLRequest(url: url)) else { return ("Linear unavailable", []) }
        request.httpMethod = "POST"; request.httpBody = Data(body.utf8); request.setValue(key, forHTTPHeaderField: "Authorization"); request.setValue("application/json", forHTTPHeaderField: "Content-Type"); request.timeoutInterval = 3
        guard let responseData = BoundedHTTP.post(request),
              let root = try? JSONSerialization.jsonObject(with: responseData) as? [String: Any],
              let nodes = (((root["data"] as? [String: Any])?["viewer"] as? [String: Any])?["assignedIssues"] as? [String: Any])?["nodes"] as? [[String: Any]] else { return ("Linear configured, but unavailable", []) }
        return ("Linear assigned to me (read-only)", nodes.compactMap { guard let id = $0["identifier"] as? String, let title = $0["title"] as? String else { return nil }; return "\(id) · \(title)" })
    }
    static func command(_ executable: String, _ arguments: [String], environment: [String: String] = [:]) -> String? {
        guard FileManager.default.isExecutableFile(atPath: executable) else { return nil }
        let process = Process(); process.executableURL = URL(fileURLWithPath: executable); process.arguments = arguments; process.environment = ProcessInfo.processInfo.environment.merging(environment) { _, new in new }
        let pipe = Pipe(); process.standardOutput = pipe; process.standardError = FileHandle.nullDevice
        let lock = NSLock(); var output = Data(); var oversized = false
        let done = DispatchSemaphore(value: 0)
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let chunk = handle.availableData; guard !chunk.isEmpty else { return }
            lock.lock(); output.append(chunk); oversized = output.count > 65_536; lock.unlock()
            if oversized { process.terminate() }
        }
        process.terminationHandler = { _ in done.signal() }
        do { try process.run() } catch { return nil }
        if done.wait(timeout: .now() + 2) == .timedOut {
            process.terminate()
            if done.wait(timeout: .now() + 0.2) == .timedOut { kill(process.processIdentifier, SIGKILL); _ = done.wait(timeout: .now() + 1) }
        }
        pipe.fileHandleForReading.readabilityHandler = nil
        lock.lock(); let data = output; let valid = !oversized && data.count <= 65_536; lock.unlock()
        guard process.terminationStatus == 0, valid else { return nil }
        return String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

private final class BoundedHTTP: NSObject, URLSessionDataDelegate, URLSessionTaskDelegate {
    private var data = Data(); private var valid = false; private let done = DispatchSemaphore(value: 0)
    static func post(_ request: URLRequest) -> Data? {
        let collector = BoundedHTTP(); let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 3; config.timeoutIntervalForResource = 3
        let session = URLSession(configuration: config, delegate: collector, delegateQueue: nil)
        session.dataTask(with: request).resume()
        guard collector.done.wait(timeout: .now() + 4) == .success, collector.valid, collector.data.count <= 65_536 else { session.invalidateAndCancel(); return nil }
        session.invalidateAndCancel(); return collector.data
    }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse, completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        guard let http = response as? HTTPURLResponse, response.url?.scheme == "https", response.url?.host == "api.linear.app", (200..<300).contains(http.statusCode), (http.value(forHTTPHeaderField: "Content-Length").flatMap(Int.init) ?? 0) <= 65_536 else { completionHandler(.cancel); return }
        valid = true; completionHandler(.allow)
    }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) { self.data.append(data); if self.data.count > 65_536 { dataTask.cancel() } }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) { if error != nil { valid = false }; done.signal() }
}

private enum Keychain {
    static func value(account: String) -> String? { let query: [CFString: Any] = [kSecClass: kSecClassGenericPassword, kSecAttrService: "com.projectmushroom.gravedecay", kSecAttrAccount: account, kSecReturnData: true]; var item: CFTypeRef?; guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { return nil }; return String(data: data, encoding: .utf8) }
    static func set(_ value: String, account: String) -> Bool { let base: [CFString: Any] = [kSecClass: kSecClassGenericPassword, kSecAttrService: "com.projectmushroom.gravedecay", kSecAttrAccount: account]; SecItemDelete(base as CFDictionary); var query = base; query[kSecValueData] = Data(value.utf8); return SecItemAdd(query as CFDictionary, nil) == errSecSuccess }
    static func remove(account: String) -> Bool { let query: [CFString: Any] = [kSecClass: kSecClassGenericPassword, kSecAttrService: "com.projectmushroom.gravedecay", kSecAttrAccount: account]; let status = SecItemDelete(query as CFDictionary); return status == errSecSuccess || status == errSecItemNotFound }
}
#endif
