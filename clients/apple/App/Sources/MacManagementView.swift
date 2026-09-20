#if os(macOS)
import SwiftUI
import GravedecayKit

struct MacManagementView: View {
    @ObservedObject var model: ManagementModel
    let destination: String?
    @State private var pendingAction: String?
    @State private var forgetPrompt = false
    @State private var localError: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if let localError { Text(localError).foregroundStyle(GraveTheme.crit) }
                if let destination, model.host == destination {
                    GravePanel("selected grave") {
                        VStack(alignment: .leading, spacing: 8) {
                            Text(destination).foregroundStyle(GraveTheme.amber).textSelection(.enabled)
                            Text("DIRECT FROM THIS MAC OVER TAILSCALE").foregroundStyle(GraveTheme.muted)
                            Link("OPEN THIS GRAVE'S DASHBOARD", destination: URL(string: "https://\(destination)/grave/")!)
                            if model.loading { ProgressView("Reading selected grave…") }
                            if let error = model.errors["connection"] { Text(error).foregroundStyle(GraveTheme.crit) }
                            if model.capabilities != nil, model.capabilities?.resource_contract == nil {
                                Text("Older grave: structured resources are not advertised. Update this destination to manage it here.")
                            }
                        }
                    }
                    resource("system", model.system) { system in
                        VStack(alignment: .leading, spacing: 8) {
                            Text("\(system.identity.hostname) // \(system.identity.os_name) // UP \(GravePresentation.uptime(system.uptime_seconds))")
                            Text("CPU \(GravePresentation.percent(system.cpu.usage_percent)) // \(system.cpu.logical_count.map(String.init) ?? "?") LOGICAL CORES")
                            Text("MEMORY \(system.memory.percent_kind.uppercased()) \(GravePresentation.percent(system.memory.usage_percent))")
                            Text("TEMPERATURE CPU \(GravePresentation.temperature(system.temperature.cpu_celsius)) // GPU \(GravePresentation.temperature(system.temperature.gpu_celsius))")
                            ForEach(system.disks) { disk in Text("DISK \(disk.mountpoint): \(GravePresentation.percent(disk.usage_percent))") }
                        }
                    }
                    resource("services", model.services) { rows in
                        if rows.isEmpty { Text("No services reported.") }
                        ForEach(rows) { row in entry(row.id, "\(row.manager) // \(knownState(row.state)) // \(row.detail ?? "—")") }
                    }
                    resource("containers", model.containers) { rows in
                        if rows.isEmpty { Text("No containers reported.") }
                        ForEach(rows) { row in entry(row.name, "\(knownState(row.state)) // \(row.status_message ?? "—") // \(row.project ?? "no project")") }
                    }
                    resource("sessions", model.sessions) { rows in
                        if rows.isEmpty { Text("No sessions reported.") }
                        ForEach(rows) { row in entry(row.name, "\(row.frozen ? "frozen" : "live") // \(row.attached.map { $0 ? "attached" : "detached" } ?? "attachment unknown") // \(row.window_count.map(String.init) ?? "?") windows // \(row.activity_label ?? "—")") }
                    }
                    resource("repositories", model.repositories) { rows in
                        if rows.isEmpty { Text("No repositories reported.") }
                        ForEach(rows) { row in entry(row.name, "\(row.branch ?? "unknown branch") // \(row.changed_files.map(String.init) ?? "?") changed files // \(row.last_commit_subject ?? "—")") }
                    }
                    preferences
                    operations
                } else {
                    Text("SELECT A GRAVE IN GRAVEYARD TO MANAGE IT.").foregroundStyle(GraveTheme.muted)
                }
            }.font(.system(size: 11, design: .monospaced)).frame(maxWidth: 980).padding(24)
        }
        .onDisappear { model.stopObserving() }
        .onChange(of: destination) { _, _ in pendingAction = nil; forgetPrompt = false; localError = nil }
        .confirmationDialog("Start \(pendingAction ?? "action") on \(destination ?? "selected grave")?", isPresented: Binding(get: { pendingAction != nil }, set: { if !$0 { pendingAction = nil } }), titleVisibility: .visible) {
            if let action = pendingAction, let destination {
                Button("START \(action.uppercased())") {
                    pendingAction = nil
                    guard model.host == destination else { return }
                    Task { guard model.host == destination else { return }; await model.start(action) }
                }
            }
        } message: { Text("This runs on the selected grave. Disconnecting does not cancel it. Reboot or mode changes may interrupt services.") }
        .confirmationDialog("Stop tracking this operation on \(destination ?? "selected grave")?", isPresented: $forgetPrompt, titleVisibility: .visible) {
            Button("STOP TRACKING", role: .destructive) { guard model.host == destination else { return }; do { try model.forgetOperation() } catch { localError = error.localizedDescription } }
        } message: { Text("This does not cancel the command. Check the grave's state before starting another action. Copy the saved ID first if you need it later.") }
    }

    private func knownState(_ state: String?) -> String {
        let known = ["active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance",
                     "running", "exited", "paused", "restarting", "dead", "created", "removing", "stopped"]
        return state.flatMap { known.contains($0) ? $0 : nil } ?? "unknown"
    }
    private func entry(_ title: String, _ detail: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).foregroundStyle(GraveTheme.ink)
            Text(detail).foregroundStyle(GraveTheme.muted)
        }.textSelection(.enabled).padding(.vertical, 3)
    }
    private func resource<T, Content: View>(_ name: String, _ resource: ManagementResource<T>?, @ViewBuilder content: (T) -> Content) -> some View {
        GravePanel(name) {
            VStack(alignment: .leading, spacing: 8) {
                if let error = model.errors[name] { Text(error).foregroundStyle(GraveTheme.amber) }
                else if let resource {
                    Text("\(resource.status.uppercased()) // \(resource.observed_at)").foregroundStyle(GraveTheme.muted)
                    if let error = resource.error { Text(error.message).foregroundStyle(GraveTheme.amber) }
                    if resource.truncated { Text("PARTIAL LIST — SERVER LIMIT REACHED").foregroundStyle(GraveTheme.amber) }
                    if ["ready", "partial"].contains(resource.status), let data = resource.data { content(data) }
                    else { Text("No current \(name) data (\(resource.status)).").foregroundStyle(GraveTheme.muted) }
                } else { Text(model.loading ? "Loading…" : "No current data.").foregroundStyle(GraveTheme.muted) }
            }
        }
    }
    private var preferences: some View {
        GravePanel("dashboard preferences // selected grave") {
            VStack(alignment: .leading, spacing: 12) {
                if let error = model.errors["preferences"] { Text(error).foregroundStyle(GraveTheme.amber) }
                if let draft = model.draft {
                    Text(model.currentPreferences == nil ? "RETAINED VALUES — NOT A CURRENT READ" : model.dirty ? "UNSAVED DRAFT" : "SAVED VALUES").foregroundStyle(GraveTheme.muted)
                    PreferenceFields(values: Binding(get: { model.draft?.values ?? draft.values }, set: { model.draft?.values = $0 }))
                        .disabled(model.saving || model.capabilities?.supports("preferences", method: "POST") != true)
                    if model.needsReview {
                        Text("DRAFT RETAINED — REVIEW CURRENT VALUES BEFORE SAVING").foregroundStyle(GraveTheme.amber)
                        Button("READ CURRENT VALUES") { Task { guard model.host == destination else { return }; await model.readCurrentPreferences() } }.buttonStyle(GraveButton())
                        if let current = model.currentPreferences {
                            DisclosureGroup("CURRENT SAVED VALUES") { PreferenceFields(values: .constant(current.values)).disabled(true) }
                            HStack {
                                Button("KEEP MY CHANGED FIELDS ON THIS REVISION") { do { try model.rebaseDraft() } catch { localError = error.localizedDescription } }
                                Button("DISCARD DRAFT & USE CURRENT") { model.useCurrentPreferences() }
                            }.buttonStyle(GraveButton()).disabled(model.saving)
                        }
                    }
                    Button(model.saving ? "SAVING…" : "SAVE PREFERENCES") { Task { guard model.host == destination else { return }; await model.savePreferences() } }
                        .buttonStyle(GraveButton()).disabled(!model.canSave)
                }
            }
        }
    }
    private var operations: some View {
        GravePanel("management operations // selected grave") {
            VStack(alignment: .leading, spacing: 10) {
                if let error = model.storageError { Text(error).foregroundStyle(GraveTheme.crit) }
                if let error = model.errors["operations"] { Text(error).foregroundStyle(GraveTheme.amber) }
                if let tracking = model.tracking {
                    Text("\(tracking.action.uppercased()) // \(tracking.id)").textSelection(.enabled)
                    Text(model.operation.map { "LAST OBSERVED: " + $0.displayState.uppercased() } ?? (tracking.finished ? "LAST ACTION — RECONNECT FOR RESULT" : "OUTCOME NOT YET VERIFIED")).foregroundStyle(GraveTheme.amber)
                    if let result = model.operation {
                        if !result.message.isEmpty { Text(result.message) }
                        if result.state == "interrupted" { Text("OUTCOME UNKNOWN — CHECK GRAVE STATE BEFORE STARTING ANOTHER ACTION") }
                        if result.truncated { Text("OUTPUT TRUNCATED BY SERVER") }
                        if let exit = result.exit_code { Text("EXIT CODE \(exit)") }
                    }
                    if !model.operationText.isEmpty {
                        ScrollView { Text(model.operationText).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                            .frame(maxHeight: 260).padding(8).background(GraveTheme.inset)
                    }
                    HStack {
                        Button(model.operationBusy ? "OBSERVING…" : "RECONNECT / VIEW RESULT") { Task { guard model.host == destination else { return }; await model.resume() } }
                            .disabled(model.operationBusy || model.capabilities?.durableActions.contains(tracking.action) != true)
                        Button("STOP TRACKING…") { forgetPrompt = true }.disabled(model.operationBusy)
                    }.buttonStyle(GraveButton())
                }
                let actions = model.capabilities?.durableActions ?? []
                if actions.isEmpty { Text("Durable management actions are not advertised by this grave. Linux single-owner graves currently support them.").foregroundStyle(GraveTheme.muted) }
                else {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 180))], alignment: .leading) {
                        ForEach(actions, id: \.self) { action in
                            Button(action.uppercased()) { pendingAction = action }.buttonStyle(GraveButton())
                                .disabled(model.operationBusy || model.tracking?.finished == false || model.storageError != nil || model.loading)
                        }
                    }
                }
            }
        }
    }
}

private struct PreferenceFields: View {
    @Binding var values: GravePreferences.Values
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Refresh interval (milliseconds)")
                TextField("2000–60000", value: $values.poll_ms, format: .number.grouping(.never)).frame(width: 100)
                Stepper("", value: $values.poll_ms, in: 2000...60000, step: 1000).labelsHidden().accessibilityLabel("Refresh interval")
            }
            Picker("T3 dashboard tile", selection: $values.t3_tile) { Text("PWA").tag("pwa"); Text("App").tag("app") }
            list("Panel order", $values.panel_order)
            list("Hidden panels", $values.hidden_panels)
            list("Hidden apps", $values.hidden_apps)
            list("New tab apps", $values.newtab_apps)
            list("Modal apps", $values.modal_apps)
            list("YOLO apps", $values.yolo_apps)
            DisclosureGroup("Custom app tiles (\(values.custom_apps.count))") {
                ForEach(values.custom_apps.indices, id: \.self) { index in
                    HStack {
                        TextField("Name", text: $values.custom_apps[index].name)
                        TextField("URL", text: $values.custom_apps[index].url)
                        Button("REMOVE") { values.custom_apps.remove(at: index) }.accessibilityLabel("Remove custom app \(index + 1)")
                    }
                }
                Button("ADD TILE") { values.custom_apps.append(.init(name: "", url: "")) }.disabled(values.custom_apps.count >= 12)
            }
        }.textFieldStyle(.roundedBorder)
    }
    private func list(_ title: String, _ entries: Binding<[String]>) -> some View {
        DisclosureGroup("\(title) (\(entries.wrappedValue.count))") {
            VStack(alignment: .leading) {
                Text("Dashboard IDs, one per row; order is preserved.").foregroundStyle(GraveTheme.muted)
                ForEach(entries.wrappedValue.indices, id: \.self) { index in
                    HStack {
                        TextField("ID", text: entries[index]).accessibilityLabel("\(title) ID \(index + 1)")
                        Button("REMOVE") { entries.wrappedValue.remove(at: index) }.accessibilityLabel("Remove \(title) ID \(index + 1)")
                    }
                }
                Button("ADD ID") { entries.wrappedValue.append("") }.disabled(entries.wrappedValue.count >= 100)
            }
        }
    }
}
#endif
