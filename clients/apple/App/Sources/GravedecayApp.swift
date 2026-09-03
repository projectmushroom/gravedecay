import SwiftUI
import GravedecayKit
#if os(macOS)
import AppKit
#endif

@main
struct GravedecayApp: App {
    @StateObject private var model = AppModel()
    #if os(macOS)
    @StateObject private var graveMenu = GraveMenuModel()
    @StateObject private var macDashboard = MacDashboardModel()
    @State private var macSelection: MacNativeContentView.Section = .graveyard
    #endif

    var body: some Scene {
        WindowGroup(id: "main") {
            #if os(macOS)
            MacNativeContentView(graves: graveMenu, model: macDashboard, selection: $macSelection)
            #else
            ContentView()
                .environmentObject(model)
            #endif
        }
        #if os(macOS)
        .defaultSize(width: 1040, height: 680)
        .windowStyle(.hiddenTitleBar)
        #endif
        #if os(macOS)
        MenuBarExtra {
            GraveMenuView(model: graveMenu)
                .onAppear { graveMenu.refresh() }
        } label: {
            Image("MenuBarSkull")
                .renderingMode(.template)
                .accessibilityLabel("Gravedecay, \(menuCondition.label)")
        }
        .menuBarExtraStyle(.window)
        Settings {
            MacSettingsView(graves: graveMenu, model: macDashboard)
                .frame(width: 520, height: 620)
                .graveRoot()
        }
        #endif
    }

    #if os(macOS)
    private var menuCondition: GravePresentation.Condition { GravePresentation.condition(summary: graveMenu.selected?.summary, reachable: graveMenu.selected?.reachable ?? false) }
    #endif
}

#if os(macOS)
extension GravePresentation.Condition {
    var label: String { switch self { case .unreachable: "Unreachable (last summary)"; case .warning: "Reachable · Warning"; case .active: "Reachable · Active"; case .frozen: "Reachable · Frozen"; case .healthy: "Reachable · Healthy" } }
    var color: Color { switch self { case .warning: GraveTheme.amber; case .active, .healthy: GraveTheme.good; case .frozen: GraveTheme.accentSoft; case .unreachable: GraveTheme.muted } }
    var icon: String { switch self { case .unreachable: "wifi.slash"; case .warning: "exclamationmark.triangle.fill"; case .active: "bolt.circle.fill"; case .frozen: "snowflake"; case .healthy: "checkmark.circle.fill" } }
}

private struct GraveMenuView: View {
    @ObservedObject var model: GraveMenuModel
    @Environment(\.openWindow) private var openWindow
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { GraveMark(color: GraveTheme.ink).accessibilityHidden(true); Text("GRAVEDECAY").tracking(1.2).foregroundStyle(GraveTheme.amber); Spacer(); Button("↻ REFRESH") { model.refresh() }.buttonStyle(GraveButton()).accessibilityLabel("Refresh graves") }.font(.system(size: 11, weight: .bold, design: .monospaced))
            Picker("TARGET", selection: $model.selectedID) { ForEach(model.graves) { Text($0.candidate.name.uppercased()).tag(Optional($0.id)) } }.pickerStyle(.menu).tint(GraveTheme.amber).font(.system(size: 10, design: .monospaced))
            if model.graves.isEmpty {
                TailscaleOnboardingView(model: model)
            } else if let grave = model.selected, let summary = grave.summary {
                VStack(alignment: .leading, spacing: 5) {
                    HStack { Rectangle().fill(condition.color).frame(width: 8, height: 8); Text(condition.label.uppercased()) }.foregroundStyle(condition.color)
                    Text("\(summary.node.host) // \(summary.node.mode) // \(summary.node.platform)").foregroundStyle(GraveTheme.muted)
                    HStack(spacing: 6) { menuTile("CPU", GravePresentation.percent(summary.resources.cpu_pct), GraveTheme.good); menuTile("MEM", GravePresentation.percent(summary.resources.memory_pct), GraveTheme.ink2); menuTile("DISK", GravePresentation.percent(summary.resources.disk_pct), GraveTheme.amber) }.accessibilityLabel("CPU \(GravePresentation.percent(summary.resources.cpu_pct)), memory \(GravePresentation.percent(summary.resources.memory_pct)), disk \(GravePresentation.percent(summary.resources.disk_pct))")
                    Text("TEMP CPU \(GravePresentation.temperature(summary.resources.cpu_temp_c)) // GPU \(GravePresentation.temperature(summary.resources.gpu_temp_c))").foregroundStyle(GraveTheme.muted)
                    Text("\(summary.activity.sessions_live) ACTIVE // \(summary.activity.sessions_frozen) FROZEN // \(summary.problems) PROBLEMS").foregroundStyle(summary.problems > 0 ? GraveTheme.crit : GraveTheme.ink2)
                    Text("UP \(GravePresentation.uptime(summary.node.uptime_s)) // SEEN \(GravePresentation.age(summary.observed_at))").foregroundStyle(GraveTheme.muted)
                    HStack { link("T3", summary.t3); if summary.terminal != nil { Button("TERMINAL") { openWindow(id: "main"); NSApp.activate(ignoringOtherApps: true); NotificationCenter.default.post(name: .openNativeTerminal, object: nil) }.buttonStyle(GraveButton()) } }
                }.font(.system(size: 9, design: .monospaced))
            } else { Text("SELECT A REACHABLE GRAVE.").font(.system(size: 9, design: .monospaced)).foregroundStyle(GraveTheme.muted) }
            Rectangle().fill(GraveTheme.ring).frame(height: 1)
            HStack { Button("OPEN APP") { openWindow(id: "main"); NSApp.activate(ignoringOtherApps: true) }; Button("QUIT") { NSApp.terminate(nil) } }.buttonStyle(GraveButton())
        }.padding().frame(width: 390).graveRoot()
    }
    private var condition: GravePresentation.Condition { GravePresentation.condition(summary: model.selected?.summary, reachable: model.selected?.reachable ?? false) }
    @ViewBuilder private func link(_ title: String, _ path: String?) -> some View { if let grave = model.selected, GravePresentation.link(host: grave.candidate.dns, path: path) != nil { Button(title.uppercased()) { model.open(path) }.buttonStyle(GraveButton()) } }
    private func menuTile(_ label: String, _ value: String, _ color: Color) -> some View { VStack(alignment: .leading, spacing: 2) { Text(label).foregroundStyle(GraveTheme.muted); Text(value).foregroundStyle(color) }.frame(maxWidth: .infinity, alignment: .leading).padding(6).background(GraveTheme.inset).overlay(Rectangle().stroke(GraveTheme.hairline)) }
}

extension Notification.Name { static let openNativeTerminal = Notification.Name("openNativeTerminal") }
#endif

struct ContentView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        #if os(iOS)
        if let box = model.box {
            TabView {
                WebPane(url: box.baseURL)
                    .ignoresSafeArea(edges: .bottom)
                    .tabItem { Label("Agents", systemImage: "brain") }

                WebPane(url: box.dashboardURL)
                    .ignoresSafeArea(edges: .bottom)
                    .tabItem { Label("Grave", systemImage: "gauge") }

                TerminalPane(box: box)
                    .tabItem { Label("Terminal", systemImage: "terminal") }

                SettingsView()
                    .tabItem { Label("Settings", systemImage: "gearshape") }
            }
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

            Button("Connect") { model.box = BoxConfig(input: hostInput); model.save() }
                .buttonStyle(.borderedProminent)
                .disabled(BoxConfig(input: hostInput) == nil)
        }
        .padding(32)
        .frame(maxWidth: 420)
    }
}

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var hostInput = ""

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
        }
        .onAppear { hostInput = model.box?.host ?? "" }
    }
}
