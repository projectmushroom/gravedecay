import SwiftUI
import GravedecayKit

@main
struct GravedecayApp: App {
    @StateObject private var model = AppModel()
    #if os(macOS)
    @StateObject private var graveMenu = GraveMenuModel()
    @StateObject private var macDashboard = MacDashboardModel()
    #endif

    var body: some Scene {
        WindowGroup(id: "main") {
            #if os(macOS)
            MacNativeContentView(graves: graveMenu, model: macDashboard)
            #else
            ContentView()
                .environmentObject(model)
            #endif
        }
        #if os(macOS)
        .defaultSize(width: 1040, height: 680)
        #endif
        #if os(macOS)
        MenuBarExtra("Gravedecay", systemImage: menuIcon) {
            GraveMenuView(model: graveMenu)
                .onAppear { graveMenu.refresh() }
        }
        .menuBarExtraStyle(.window)
        Settings {
            MacSettingsView(model: macDashboard)
                .frame(width: 420)
                .padding()
        }
        #endif
    }

    #if os(macOS)
    private var menuIcon: String { GraveMenuView.icon(for: GravePresentation.condition(summary: graveMenu.selected?.summary, reachable: graveMenu.selected?.reachable ?? false)) }
    #endif
}

#if os(macOS)
private struct GraveMenuView: View {
    @ObservedObject var model: GraveMenuModel
    @Environment(\.openWindow) private var openWindow
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { Label("Gravedecay", systemImage: "cross.case.fill").font(.headline); Spacer(); Button("Refresh", systemImage: "arrow.clockwise") { model.refresh() }.accessibilityLabel("Refresh appliances") }
            if model.graves.count > 1 { Picker("Appliance", selection: $model.selectedID) { ForEach(model.graves) { Text($0.candidate.name).tag(Optional($0.id)) } }.pickerStyle(.menu) }
            if let grave = model.selected, let summary = grave.summary {
                VStack(alignment: .leading, spacing: 5) {
                    Label(title(for: condition), systemImage: Self.icon(for: condition)).foregroundStyle(color(for: condition))
                    Text("\(summary.node.host) · \(summary.node.mode) · \(summary.node.platform)").font(.subheadline)
                    Text("CPU \(GravePresentation.percent(summary.resources.cpu_pct))   Memory \(GravePresentation.percent(summary.resources.memory_pct))   Disk \(GravePresentation.percent(summary.resources.disk_pct))").accessibilityLabel("CPU \(GravePresentation.percent(summary.resources.cpu_pct)), memory \(GravePresentation.percent(summary.resources.memory_pct)), disk \(GravePresentation.percent(summary.resources.disk_pct))")
                    Text("Temp CPU \(GravePresentation.temperature(summary.resources.cpu_temp_c)) · GPU \(GravePresentation.temperature(summary.resources.gpu_temp_c))")
                    Text("\(summary.activity.sessions_live) active · \(summary.activity.sessions_frozen) frozen · \(summary.problems) problems")
                    Text("Uptime \(GravePresentation.uptime(summary.node.uptime_s)) · observed \(GravePresentation.age(summary.observed_at))").foregroundStyle(.secondary)
                    HStack { link("Dashboard", summary.links.dashboard); link("T3", summary.links.t3); link("Terminal", summary.links.terminal); link("Network", summary.links.network) }
                }
            } else { Text(message).foregroundStyle(.secondary) }
            Divider()
            Button("Open Gravedecay") { openWindow(id: "main"); NSApp.activate(ignoringOtherApps: true) }
            Button("Quit Gravedecay") { NSApp.terminate(nil) }
        }.padding().frame(width: 390).graveRoot()
    }
    private var condition: GravePresentation.Condition { GravePresentation.condition(summary: model.selected?.summary, reachable: model.selected?.reachable ?? false) }
    static func icon(for condition: GravePresentation.Condition) -> String { switch condition { case .unreachable: return "wifi.slash"; case .warning: return "exclamationmark.triangle.fill"; case .active: return "bolt.circle.fill"; case .frozen: return "snowflake"; case .healthy: return "checkmark.circle.fill" } }
    private func title(for condition: GravePresentation.Condition) -> String { switch condition { case .unreachable: return "Unreachable (last summary)"; case .warning: return "Reachable · Warning"; case .active: return "Reachable · Active"; case .frozen: return "Reachable · Frozen"; case .healthy: return "Reachable · Healthy" } }
    private func color(for condition: GravePresentation.Condition) -> Color { switch condition { case .warning: return .orange; case .active: return .blue; case .frozen: return .cyan; case .healthy: return .green; case .unreachable: return .secondary } }
    private var message: String { switch model.state { case .scanning: return "Discovering tailnet appliances…"; case .missingTailscale: return "Tailscale CLI not found. Install the Tailscale app."; case .loggedOut: return "Tailscale is logged out. Sign in with the Tailscale app."; case .noAppliances: return "No reachable Gravedecay appliances found."; default: return "Waiting to discover appliances…" } }
    @ViewBuilder private func link(_ title: String, _ path: String?) -> some View { if let grave = model.selected, GravePresentation.link(host: grave.candidate.dns, path: path) != nil { Button(title) { model.open(path) } } }
}
#endif

struct ContentView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        #if os(iOS)
        if let box = model.box {
            TabView {
                WebPane(url: box.t3URL, proxy: model.proxy)
                    .ignoresSafeArea(edges: .bottom)
                    .tabItem { Label("Agents", systemImage: "brain") }

                WebPane(url: box.dashboardURL, proxy: model.proxy)
                    .ignoresSafeArea(edges: .bottom)
                    .tabItem { Label("Grave", systemImage: "gauge") }

                TerminalPane(box: box, urlSession: model.urlSession)
                    .tabItem { Label("Terminal", systemImage: "terminal") }

                #if os(iOS)
                SettingsView()
                    .tabItem { Label("Settings", systemImage: "gearshape") }
                #endif
            }
            // Recreate the panes when the proxy appears/changes so webviews
            // and the terminal pick up the new route.
            .id(model.proxy)
        } else {
            SetupView()
        }
        #else
        EmptyView()
        #endif
    }
}

struct SetupView: View {
    @EnvironmentObject private var model: AppModel
    @State private var hostInput = ""
    @State private var authKey = ""
    @State private var mode: TailnetMode = .system

    var body: some View {
        VStack(spacing: 16) {
            Text("🪦").font(.system(size: 56))
            Text("gravedecay").font(.largeTitle.bold())
            Text("Point this app at your box's tailnet name.")
                .foregroundStyle(.secondary)

            TextField("box.tailnet-name.ts.net", text: $hostInput)
                .textFieldStyle(.roundedBorder)
                .autocorrectionDisabled()
                #if os(iOS)
                .textInputAutocapitalization(.never)
                .keyboardType(.URL)
                #endif

            if AppModel.embeddedAvailable {
                Picker("Connectivity", selection: $mode) {
                    ForEach(TailnetMode.allCases) { Text($0.label).tag($0) }
                }
                .pickerStyle(.segmented)

                if mode == .embedded {
                    SecureField("Tailscale auth key (first join only)", text: $authKey)
                        .textFieldStyle(.roundedBorder)
                }
            }

            Button("Connect", action: connect)
                .buttonStyle(.borderedProminent)
                .disabled(BoxConfig(input: hostInput) == nil)

            if let error = model.tailnetError {
                Text(error).font(.footnote).foregroundStyle(.red)
            }
        }
        .padding(32)
        .frame(maxWidth: 420)
    }

    private func connect() {
        guard let box = BoxConfig(input: hostInput) else { return }
        model.box = box
        model.mode = mode
        model.save()
        if mode == .embedded {
            let key = authKey
            authKey = ""
            Task { await model.startEmbedded(authKey: key) }
        }
    }
}

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var hostInput = ""
    @State private var authKey = ""

    var body: some View {
        Form {
            Section("Box") {
                TextField("host", text: $hostInput)
                    .autocorrectionDisabled()
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    #endif
                Button("Save") {
                    if let box = BoxConfig(input: hostInput) {
                        model.box = box
                        model.save()
                    }
                }
                .disabled(BoxConfig(input: hostInput) == nil)
            }

            Section("Tailnet") {
                if AppModel.embeddedAvailable {
                    Picker("Mode", selection: $model.mode) {
                        ForEach(TailnetMode.allCases) { Text($0.label).tag($0) }
                    }
                    .onChange(of: model.mode) { _, _ in model.save() }

                    if model.mode == .embedded {
                        SecureField("Auth key (first join only)", text: $authKey)
                        Button(model.tailnetBusy ? "Joining…" : "Join tailnet") {
                            let key = authKey
                            authKey = ""
                            Task { await model.startEmbedded(authKey: key) }
                        }
                        .disabled(model.tailnetBusy)

                        if model.proxy != nil {
                            Label("Embedded node up", systemImage: "checkmark.circle.fill")
                                .foregroundStyle(.green)
                        }
                        if let error = model.tailnetError {
                            Text(error).font(.footnote).foregroundStyle(.red)
                        }
                    }
                } else {
                    Text("Built without TailscaleKit — connectivity comes from " +
                         "the Tailscale app's VPN. See clients/apple/README.md " +
                         "for the embedded build.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                }
            }
        }
        .onAppear { hostInput = model.box?.host ?? "" }
    }
}
