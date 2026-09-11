#if os(macOS)
import AppKit
import Combine
import Sparkle

/// One updater serves the native UI and the owner-authenticated web dashboard.
/// Sparkle verifies feeds/archives and replaces the complete app bundle.
@MainActor
final class MacAppUpdates: NSObject, ObservableObject, SPUUpdaterDelegate, SPUUserDriver {
    static let shared = MacAppUpdates()
    @Published private(set) var releases: [String] = []
    @Published private(set) var latest = ""
    @Published private(set) var message = "Checking for app updates…"
    @Published private(set) var state = "idle"
    @Published private(set) var checking = false
    let current = "v" + (Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "0.0.0")
    var available: Bool { !latest.isEmpty && !busy }
    var busy: Bool { checking || state == "queued" || state == "running" }
    var beforeRelaunch: (() -> Void)?
    private let directory: URL
    private lazy var updater = SPUUpdater(hostBundle: .main, applicationBundle: .main, userDriver: self, delegate: self)
    private var selected: String?
    private var attempt = ""
    private var target = ""
    private var timer: Timer?
    private var lastCheck = Date.distantPast
    private var lastWrite = Date.distantPast
    private var started = false

    init(directory: URL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Gravedecay/NativeHost/updates")) {
        self.directory = directory
        super.init()
    }

    func start() {
        guard !started else { return }
        started = true
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            if let data = try? Data(contentsOf: directory.appendingPathComponent("status.json")),
               let previous = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                attempt = previous["attempt"] as? String ?? ""
                target = previous["target"] as? String ?? ""
                if !attempt.isEmpty {
                    state = target == current ? "ok" : "failed"
                    message = target == current ? "App updated to \(current)" : "The app restarted without confirming the requested release. Check again before retrying."
                }
            }
            try updater.start()
            save()
            timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
                Task { @MainActor in self?.tick() }
            }
        } catch { state = "failed"; message = error.localizedDescription; save() }
    }

    private func tick() {
        let request = directory.appendingPathComponent("request.json")
        if let data = try? Data(contentsOf: request), data.count <= 4096,
           let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            // Publish the reservation before removing the mailbox so a second
            // browser cannot overwrite an accepted request during a download.
            if !busy && updater.canCheckForUpdates {
                let tag = value["tag"] as? String ?? ""
                if value["action"] as? String == "check" {
                    check()
                    try? FileManager.default.removeItem(at: request)
                    return
                }
                attempt = value["attempt"] as? String ?? UUID().uuidString
                target = tag
                if value["current"] as? String == current,
                   let requested = value["requested_at"] as? Double,
                   abs(Date().timeIntervalSince1970 - requested) < 60, releases.contains(tag) {
                    install(tag, attempt: attempt)
                } else {
                    state = "failed"; message = "Update request expired or the release is no longer offered."; save()
                }
                try? FileManager.default.removeItem(at: request)
            }
        }
        if Date().timeIntervalSince(lastWrite) > 10 { save() }
        if !busy && Date().timeIntervalSince(lastCheck) > 3600 { check() }
    }

    func check() {
        guard !busy, updater.canCheckForUpdates else { return }
        checking = true; lastCheck = Date(); message = "Checking for app updates…"; save()
        updater.checkForUpdateInformation()
    }

    func install(_ tag: String, attempt requestID: String = UUID().uuidString) {
        guard !busy, updater.canCheckForUpdates, releases.contains(tag) else { return }
        selected = tag; target = tag; attempt = requestID
        state = "running"; message = "Preparing \(tag)…"; save()
        updater.checkForUpdates()
    }

    func confirmInstall(_ tag: String) {
        let alert = NSAlert()
        alert.messageText = "Update this Mac to \(tag)?"
        alert.informativeText = "Gravedecay will download the update and restart. Devices using this Mac’s dashboard will briefly disconnect, then reconnect."
        alert.addButton(withTitle: "Update & Restart")
        alert.addButton(withTitle: "Cancel")
        if alert.runModal() == .alertFirstButtonReturn { install(tag) }
    }

    private func save() {
        let value: [String: Any] = ["state": state, "message": message, "log": message,
            "attempt": attempt, "target": target, "current": current, "checkout": current,
            "channel": "release", "releases": releases, "latest": latest,
            "available": available, "checking": checking, "updated_at": Date().timeIntervalSince1970]
        if let data = try? JSONSerialization.data(withJSONObject: value) {
            try? data.write(to: directory.appendingPathComponent("status.json"), options: .atomic)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: directory.appendingPathComponent("status.json").path)
        }
        lastWrite = Date()
    }

    func updater(_ updater: SPUUpdater, didFinishLoading appcast: SUAppcast) {
        releases = appcast.items.compactMap { item in
            let version = item.versionString
            guard version.range(of: "^[0-9]+\\.[0-9]+\\.[0-9]+$", options: .regularExpression) != nil,
                  !item.isInformationOnlyUpdate,
                  SUStandardVersionComparator.default.compareVersion(String(current.dropFirst()), toVersion: version) == .orderedAscending else { return nil }
            return "v" + version
        }.sorted { SUStandardVersionComparator.default.compareVersion($0, toVersion: $1) == .orderedDescending }
        if selected == nil { latest = "" }
        save()
    }

    func updater(_ updater: SPUUpdater, didFindValidUpdate item: SUAppcastItem) {
        if selected == nil { latest = "v" + item.versionString }
        save()
    }

    func bestValidUpdate(in appcast: SUAppcast, for updater: SPUUpdater) -> SUAppcastItem? {
        guard let selected else { return nil }
        return appcast.items.first { "v" + $0.versionString == selected } ?? SUAppcastItem.empty()
    }

    func updater(_ updater: SPUUpdater, didFinishUpdateCycleFor updateCheck: SPUUpdateCheck, error: Error?) {
        checking = false
        if state != "running" {
            message = error?.localizedDescription ?? (latest.isEmpty ? "You’re up to date." : "\(latest) is available")
        }
        if let error, selected != nil { state = "failed"; message = error.localizedDescription }
        selected = nil
        save()
    }

    func updaterWillRelaunchApplication(_ updater: SPUUpdater) { beforeRelaunch?() }
    func updater(_ updater: SPUUpdater, shouldDownloadReleaseNotesForUpdate item: SUAppcastItem) -> Bool { false }

    // A custom driver lets an explicit phone/PWA action complete without a
    // second dialog waiting unseen on the host Mac. Both UIs confirm first.
    func show(_ request: SPUUpdatePermissionRequest, reply: @escaping (SUUpdatePermissionResponse) -> Void) {
        reply(SUUpdatePermissionResponse(automaticUpdateChecks: false, sendSystemProfile: false))
    }
    func showUserInitiatedUpdateCheck(cancellation: @escaping () -> Void) {}
    func showUpdateFound(with appcastItem: SUAppcastItem, state: SPUUserUpdateState, reply: @escaping (SPUUserUpdateChoice) -> Void) {
        guard selected == "v" + appcastItem.versionString, !appcastItem.isInformationOnlyUpdate else { reply(.dismiss); return }
        message = "Downloading \(target)…"; save(); reply(.install)
    }
    func showUpdateReleaseNotes(with downloadData: SPUDownloadData) {}
    func showUpdateReleaseNotesFailedToDownloadWithError(_ error: Error) {}
    func showUpdateNotFoundWithError(_ error: Error, acknowledgement: @escaping () -> Void) {
        if selected != nil { state = "failed"; message = "The selected release is not compatible with this Mac." }
        save(); acknowledgement()
    }
    func showUpdaterError(_ error: Error, acknowledgement: @escaping () -> Void) {
        state = "failed"; message = error.localizedDescription; save(); acknowledgement()
    }
    func showDownloadInitiated(cancellation: @escaping () -> Void) {}
    func showDownloadDidReceiveExpectedContentLength(_ expectedContentLength: UInt64) {}
    func showDownloadDidReceiveData(ofLength length: UInt64) {}
    func showDownloadDidStartExtractingUpdate() { message = "Verifying and preparing \(target)…"; save() }
    func showExtractionReceivedProgress(_ progress: Double) {}
    func showReady(toInstallAndRelaunch reply: @escaping (SPUUserUpdateChoice) -> Void) {
        message = "Restarting to install \(target)…"; save(); reply(.install)
    }
    func showInstallingUpdate(withApplicationTerminated applicationTerminated: Bool, retryTerminatingApplication: @escaping () -> Void) {}
    func showUpdateInstalledAndRelaunched(_ relaunched: Bool, acknowledgement: @escaping () -> Void) { acknowledgement() }
    func dismissUpdateInstallation() {}
    func showUpdateInFocus() {}
}

#endif
