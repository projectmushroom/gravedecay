#if os(macOS)
import AppKit
import Foundation
import GravedecayKit

/// Owns the bundled dashboard/network process. No external companion or Python
/// installation is needed; stdin ties the child's lifetime to this app.
@MainActor
final class MacNativeHost: ObservableObject {
    enum State: Equatable { case off, starting, hosted, existingCompanion, unavailable }
    @Published private(set) var state: State = .off
    @Published private(set) var detail = "OFF // THIS MAC IS NOT SHARED"
    private let defaults: UserDefaults
    private var process: Process?
    private var input: Pipe?
    private var output: Pipe?
    private var log: FileHandle?
    private var activity: NSObjectProtocol?
    private var startup: Task<Void, Never>?
    private var readyData = Data()
    private var restoreAttempted = false
    private var requested: Bool
    var hostRequested: Bool { requested }
    let root = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Gravedecay/NativeHost")

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        requested = defaults.bool(forKey: "nativeHostEnabled")
    }

    func restoreIfRequested() {
        guard !restoreAttempted, requested else { return }
        restoreAttempted = true
        enable()
    }

    func enable() {
        guard process == nil, state != .starting else { return }
        guard !Self.legacyCompanionActive() else {
            state = .existingCompanion
            detail = "CLASSIC COMPANION ACTIVE // STOP IT BEFORE SWITCHING HOSTS"
            return
        }
        state = .starting
        detail = "STARTING DASHBOARD AND NETWORK…"
        startup = Task { [weak self] in
            let probe = await GraveMenuModel.tailscaleStatus()
            guard let self, !Task.isCancelled else { return }
            let currentOwner = probe.data.flatMap(MacHostIdentity.owner)
            // Keep the identity authorized when hosting was enabled. A tailnet
            // account switch must not silently grant a new account private work.
            let owner = self.defaults.string(forKey: "nativeHostOwner") ?? currentOwner
            guard let owner else { self.fail("SIGN INTO TAILSCALE, THEN RETRY LOCAL HOST"); return }
            do { try self.launch(owner: owner) }
            catch { self.fail("COULD NOT START HOST: \(error.localizedDescription)") }
        }
    }

    private func launch(owner: String) throws {
        guard let resources = Bundle.main.resourceURL?.appendingPathComponent("NativeHost") else {
            throw CocoaError(.fileNoSuchFile)
        }
        #if arch(arm64)
        let architecture = "aarch64"
        #else
        let architecture = "x86_64"
        #endif
        let executable = resources.appendingPathComponent("\(architecture)/python/bin/python3")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            fail("BUNDLED HOST RUNTIME MISSING // REINSTALL THE MAC APP")
            return
        }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let logURL = root.appendingPathComponent("host.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        let log = try FileHandle(forWritingTo: logURL)
        if try log.seekToEnd() > 1_048_576 { try log.truncate(atOffset: 0) }
        self.log = log
        let child = Process(), input = Pipe(), output = Pipe()
        child.executableURL = executable
        child.arguments = ["-I", "-B", "-u", resources.appendingPathComponent("host.py").path, "--root", root.path]
        child.environment = ["HOME": NSHomeDirectory(), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                             "TMPDIR": NSTemporaryDirectory(), "LANG": "en_US.UTF-8", "TAILSCALE_BE_CLI": "1",
                             "GRAVEDECAY_ALLOWED_USERS": owner]
        child.standardInput = input
        child.standardOutput = output
        child.standardError = log
        self.process = child; self.input = input; self.output = output; readyData = Data()
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            Task { @MainActor in
                guard let self, self.process === child else { return }
                self.readyData.append(data)
                guard self.readyData.count <= 4096 else { self.fail("INVALID HOST STARTUP RESPONSE"); return }
                guard let ready = try? JSONSerialization.jsonObject(with: self.readyData) as? [String: Any],
                      ready["ready"] as? Bool == true, ready["port"] as? Int == 4712,
                      ready["network_port"] as? Int == 4714 else { return }
                output.fileHandleForReading.readabilityHandler = nil
                self.defaults.set(owner, forKey: "nativeHostOwner")
                self.defaults.set(true, forKey: "nativeHostEnabled")
                self.requested = true; self.state = .hosted
                self.detail = "HOSTING DASHBOARD + NETWORK // READY FOR YOUR DEVICES"
                self.activity = ProcessInfo.processInfo.beginActivity(options: [.userInitiated, .idleSystemSleepDisabled], reason: "Serving Gravedecay to tailnet devices")
            }
        }
        child.terminationHandler = { [weak self] child in Task { @MainActor in
            guard let self, self.process === child else { return }
            self.fail("HOST STOPPED (\(child.terminationStatus)) // CHECK HOST LOG; PORTS 4712 AND 4714 MUST BE FREE")
        }}
        try child.run()
        // An idle parent retains only the write end. Its crash also delivers EOF.
        try? input.fileHandleForReading.close()
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 45_000_000_000)
            guard let self, self.process === child, self.state == .starting else { return }
            self.fail("HOST STARTUP TIMED OUT // CHECK HOST LOG")
        }
    }

    private func stopProcess() {
        startup?.cancel(); startup = nil
        let child = process; process = nil
        output?.fileHandleForReading.readabilityHandler = nil
        try? input?.fileHandleForWriting.close(); input = nil; output = nil
        if child?.isRunning == true { child?.terminate() }
        try? log?.close(); log = nil
        if let activity { ProcessInfo.processInfo.endActivity(activity); self.activity = nil }
    }

    private func fail(_ message: String) {
        stopProcess(); state = .unavailable; detail = message
    }

    func disable() {
        stopProcess(); requested = false
        defaults.set(false, forKey: "nativeHostEnabled")
        defaults.removeObject(forKey: "nativeHostOwner")
        state = .off; detail = "OFF // THIS MAC IS NOT SHARED"
    }

    // Keep the opted-in host preference so the replacement app restores it.
    func pauseForUpdate() { stopProcess() }

    func openDashboard() {
        guard state == .hosted else { return }
        Task {
            let probe = await GraveMenuModel.tailscaleStatus()
            guard let data = probe.data, let status = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let own = status["Self"] as? [String: Any], let dns = own["DNSName"] as? String,
                  let url = GravePresentation.link(host: dns, path: "/grave/") else { return }
            NSWorkspace.shared.open(url)
        }
    }

    func openLog() { NSWorkspace.shared.open(root.appendingPathComponent("host.log")) }
    var manualServeCommand: String {
        "tailscale serve --bg --https=443 --set-path=/grave http://127.0.0.1:4712\ntailscale serve --bg --https=443 --set-path=/net http://127.0.0.1:4714"
    }

    private static func legacyCompanionActive() -> Bool {
        for label in ["io.gravedecay.dashboard", "io.gravedecay.network"] {
            let process = Process(); process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            process.arguments = ["print", "gui/\(getuid())/\(label)"]
            process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
            if (try? process.run()) != nil { process.waitUntilExit(); if process.terminationStatus == 0 { return true } }
        }
        return false
    }
}
#endif
