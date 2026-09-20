#if os(macOS)
import Foundation
import Combine

/// Window state is separate from the local Mac collectors. Every asynchronous
/// result carries its original destination and selection generation.
@MainActor
public final class ManagementModel: ObservableObject {
    @Published public private(set) var host: String?
    @Published public private(set) var capabilities: ManagementCapabilities?
    @Published public private(set) var system: ManagementResource<GraveSystem>?
    @Published public private(set) var services: ManagementResource<[GraveService]>?
    @Published public private(set) var containers: ManagementResource<[GraveContainer]>?
    @Published public private(set) var sessions: ManagementResource<[GraveSession]>?
    @Published public private(set) var repositories: ManagementResource<[GraveRepository]>?
    @Published public private(set) var errors: [String: String] = [:]
    @Published public private(set) var loading = false
    @Published public var draft: GravePreferences?
    @Published public private(set) var currentPreferences: GravePreferences?
    @Published public private(set) var needsReview = false
    @Published public private(set) var saving = false
    @Published public private(set) var operation: GraveOperation?
    @Published public private(set) var operationText = ""
    @Published public private(set) var operationBusy = false
    @Published public private(set) var tracked: [String: Tracking] = [:]
    @Published public private(set) var storageError: String?

    public struct Tracking: Codable, Equatable, Sendable {
        public let id, action: String
        public var acknowledged = false
        public var finished = false
    }
    public var tracking: Tracking? { host.flatMap { tracked[$0] } }
    public var dirty: Bool { draft?.values != basePreferences?.values }
    public var canSave: Bool { dirty && !needsReview && !saving && !loading && capabilities?.supports("preferences", method: "POST") == true }
    private var basePreferences: GravePreferences?
    private var drafts: [String: (base: GravePreferences?, draft: GravePreferences?, review: Bool)] = [:]
    private var generation = UUID()
    private var loadGeneration = UUID()
    private var client: ManagementAPI?
    private var progressTask: Task<Void, Never>?
    private let trackingURL: URL
    private let makeClient: (String) throws -> ManagementAPI

    public init(trackingURL: URL? = nil, makeClient: @escaping (String) throws -> ManagementAPI = { try ManagementAPI(host: $0) }) {
        self.makeClient = makeClient
        self.trackingURL = trackingURL ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Gravedecay/management-operations.json")
        if FileManager.default.fileExists(atPath: self.trackingURL.path) {
            do {
                let data = try Data(contentsOf: self.trackingURL)
                guard data.count <= 262_144 else { throw ManagementError("Operation tracking is too large.") }
                let saved = try JSONDecoder().decode([String: Tracking].self, from: data)
                guard saved.count <= 128, saved.allSatisfy({ ManagementAPI.tailnetHost($0.key) == $0.key &&
                    $0.value.id.range(of: "^[0-9]{10}-[0-9a-f]{32}$", options: .regularExpression) != nil }) else {
                    throw ManagementError("Invalid saved operation destination or ID.")
                }
                tracked = saved
            } catch { storageError = "Cannot read operation tracking. New actions are disabled: \(error.localizedDescription)" }
        }
    }

    public func select(_ destination: String?) {
        guard destination != host else { return }
        if let host { drafts[host] = (basePreferences, draft, needsReview) }
        generation = UUID(); loadGeneration = UUID(); progressTask?.cancel(); progressTask = nil
        host = destination; client = nil; capabilities = nil
        system = nil; services = nil; containers = nil; sessions = nil; repositories = nil
        errors = [:]; loading = false; saving = false; operationBusy = false
        currentPreferences = nil; operation = nil; operationText = ""
        let saved = destination.flatMap { drafts[$0] }
        basePreferences = saved?.base; draft = saved?.draft; needsReview = saved?.review ?? false
        if let destination {
            do { client = try makeClient(destination) }
            catch { errors["connection"] = error.localizedDescription }
        }
    }

    public func refresh() async {
        guard let client, !saving else { return }
        currentPreferences = nil
        let token = generation, load = UUID(); loadGeneration = load
        loading = true; capabilities = nil; errors = [:]
        system = nil; services = nil; containers = nil; sessions = nil; repositories = nil
        defer { if generation == token && loadGeneration == load { loading = false } }
        do {
            let caps = try await client.capabilities()
            guard generation == token, loadGeneration == load, !Task.isCancelled else { return }
            capabilities = caps
            // Independent resource failures do not hide healthy siblings.
            async let a: Void = read("system", as: GraveSystem.self, client: client, token: token, load: load) { self.system = $0 }
            async let b: Void = read("services", as: [GraveService].self, client: client, token: token, load: load) { self.services = $0 }
            async let c: Void = read("containers", as: [GraveContainer].self, client: client, token: token, load: load) { self.containers = $0 }
            async let d: Void = read("sessions", as: [GraveSession].self, client: client, token: token, load: load) { self.sessions = $0 }
            async let e: Void = read("repositories", as: [GraveRepository].self, client: client, token: token, load: load) { self.repositories = $0 }
            async let f: Void = read("preferences", as: GravePreferences.self, client: client, token: token, load: load) { resource in
                guard resource.status == "ready", let prefs = resource.data else {
                    self.errors["preferences"] = resource.error?.message ?? "Preferences are \(resource.status)."; return
                }
                self.currentPreferences = prefs
                if self.draft == nil || (!self.dirty && !self.saving) { self.basePreferences = prefs; self.draft = prefs }
                else if self.basePreferences?.revision != prefs.revision { self.needsReview = true }
            }
            _ = await (a, b, c, d, e, f)
        } catch {
            guard generation == token, loadGeneration == load, !Task.isCancelled else { return }
            errors["connection"] = error.localizedDescription
        }
    }
    private func read<T: Decodable & Sendable>(_ name: String, as type: T.Type, client: ManagementAPI, token: UUID, load: UUID,
                                               apply: @MainActor (ManagementResource<T>) -> Void) async {
        guard generation == token, loadGeneration == load, !Task.isCancelled else { return }
        guard capabilities?.supports(name) == true else { errors[name] = "Not advertised by this grave. Update it or use its dashboard."; return }
        do {
            let resource = try await client.resource(name, as: type)
            guard generation == token, loadGeneration == load, !Task.isCancelled else { return }
            apply(resource)
        } catch {
            guard generation == token, loadGeneration == load, !Task.isCancelled else { return }
            errors[name] = error.localizedDescription
        }
    }

    public func savePreferences() async {
        guard canSave, let client, let base = basePreferences, let submitted = draft else { return }
        let token = generation; saving = true
        defer { if generation == token { saving = false } }
        do {
            let reply = try await client.savePreferences(base: base, values: submitted.values)
            guard generation == token, let prefs = reply.data else { return }
            basePreferences = prefs; currentPreferences = prefs; draft = prefs; needsReview = false
            errors["preferences"] = nil
        } catch {
            guard generation == token else { return }
            // Both a conflict and a lost success response need a read/review,
            // never an unchecked retry against a newly fetched revision.
            needsReview = true; currentPreferences = nil
            errors["preferences"] = "\(error.localizedDescription) Draft retained. Read current values and review before saving again."
        }
    }
    public func readCurrentPreferences() async {
        guard let client else { return }
        let token = generation
        currentPreferences = nil
        do {
            let reply = try await client.resource("preferences", as: GravePreferences.self)
            guard generation == token else { return }
            guard reply.status == "ready", let prefs = reply.data else {
                errors["preferences"] = reply.error?.message ?? "Current preferences are \(reply.status). Draft retained."; return
            }
            currentPreferences = prefs; errors["preferences"] = nil
        } catch { if generation == token { errors["preferences"] = error.localizedDescription } }
    }
    public func useCurrentPreferences() {
        guard !saving, let currentPreferences else { return }
        basePreferences = currentPreferences; draft = currentPreferences; needsReview = false
    }
    public func rebaseDraft() throws {
        guard !saving, let currentPreferences, let base = basePreferences, let draft else { return }
        let encoder = JSONEncoder()
        let old = try JSONSerialization.jsonObject(with: encoder.encode(base.values)) as! [String: NSObject]
        let edited = try JSONSerialization.jsonObject(with: encoder.encode(draft.values)) as! [String: NSObject]
        var merged = try JSONSerialization.jsonObject(with: encoder.encode(currentPreferences.values)) as! [String: NSObject]
        for (key, value) in edited where value != old[key] { merged[key] = value }
        let values = try JSONDecoder().decode(GravePreferences.Values.self, from: JSONSerialization.data(withJSONObject: merged))
        basePreferences = currentPreferences
        self.draft = GravePreferences(revision: currentPreferences.revision, values: values); needsReview = false
    }

    private func persist(_ record: Tracking?, host: String) throws {
        guard storageError == nil else { throw ManagementError(storageError!) }
        var saved = tracked; saved[host] = record
        guard saved.count <= 128 else { throw ManagementError("Saved operation destination limit reached (128).") }
        let directory = trackingURL.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let data = try JSONEncoder().encode(saved)
        // Atomic replacement with private permissions, before a request can run.
        try data.write(to: trackingURL, options: [.atomic])
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: trackingURL.path)
        tracked = saved
    }
    public func start(_ action: String) async {
        guard let client, let host, capabilities?.durableActions.contains(action) == true,
              !operationBusy, tracking?.finished != false, storageError == nil else { return }
        let record = Tracking(id: "\(Int(Date().timeIntervalSince1970))-\(UUID().uuidString.lowercased().replacingOccurrences(of: "-", with: ""))", action: action)
        do { try persist(record, host: host) }
        catch { errors["operations"] = "Action not sent: \(error.localizedDescription)"; return }
        operation = nil; operationText = ""
        await observe(client: client, destination: host, record: record, submit: true)
    }
    public func resume() async {
        guard let client, let host, let tracking, !operationBusy,
              capabilities?.durableActions.contains(tracking.action) == true else { return }
        operation = nil; operationText = ""
        await observe(client: client, destination: host, record: tracking, submit: false)
    }
    public func stopObserving() { progressTask?.cancel() }
    public func forgetOperation() throws {
        guard !operationBusy, let host else { return }
        try persist(nil, host: host); operation = nil; operationText = ""; errors["operations"] = nil
    }
    private func observe(client: ManagementAPI, destination: String, record: Tracking, submit: Bool) async {
        let token = generation
        operationBusy = true; errors["operations"] = nil
        progressTask?.cancel()
        let task = Task { @MainActor in
            defer { if self.generation == token { self.operationBusy = false } }
            var current = record
            do {
                var reply: GraveOperation
                if submit { reply = try await client.start(id: record.id, action: record.action) }
                else {
                    do { reply = try await client.operation(id: record.id) }
                    catch let error as ManagementError where error.status == 404 && error.code == "not_found" && !record.acknowledged {
                        try Task.checkCancellation()
                        guard self.generation == token else { return }
                        reply = try await client.start(id: record.id, action: record.action)
                    }
                }
                while true {
                    guard reply.id == record.id, reply.action == record.action else { throw ManagementError("Operation response did not match the saved ID and action.") }
                    current.acknowledged = true; current.finished = !reply.active
                    // Persist acknowledgements to their original host even if the
                    // selection changed while the server accepted the request.
                    if self.tracked[destination]?.id == record.id {
                        do { try self.persist(current, host: destination) }
                        catch {
                            self.tracked[destination] = current
                            self.storageError = "Cannot save operation acknowledgement. New actions are disabled: \(error.localizedDescription)"
                            throw error
                        }
                    }
                    guard self.generation == token, !Task.isCancelled else { return }
                    self.operation = reply
                    self.operationText += reply.events.map(\.text).joined()
                    if self.operationText.utf8.count > 131_072 { self.operationText = String(self.operationText.suffix(32_768)) }
                    guard reply.active else { return }
                    try await Task.sleep(nanoseconds: 1_000_000_000)
                    reply = try await client.operation(id: record.id, after: reply.cursor)
                }
            } catch {
                guard self.generation == token, !Task.isCancelled else { return }
                self.errors["operations"] = "\(error.localizedDescription) Saved ID retained. Reconnect to check progress before starting a replacement."
            }
        }
        progressTask = task
        await task.value
    }
}
#endif
