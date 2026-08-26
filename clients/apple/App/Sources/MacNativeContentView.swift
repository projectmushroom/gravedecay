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
    @ObservedObject var host: MacNativeHost
    @Binding var selection: Section

    enum Section: Hashable { case graveyard, thisMac, work, network, terminal, settings }

    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 8) {
                GraveMark(color: GraveTheme.ink, size: 36).shadow(color: GraveTheme.good.opacity(0.45), radius: 8).accessibilityHidden(true)
                Text("gravedecay").font(.system(size: 15, weight: .bold, design: .monospaced)).foregroundStyle(GraveTheme.ink)
                Text("NATIVE MACOS CLIENT").font(.system(size: 9, design: .monospaced)).foregroundStyle(GraveTheme.muted).padding(.bottom, 18)
                ForEach([(Section.graveyard,"GRAVEYARD","server.rack"),(Section.thisMac,"THIS MAC","desktopcomputer"),(Section.work,"WORK","folder"),(Section.network,"NETWORK","network"),(Section.terminal,"TERMINAL","terminal"),(Section.settings,"SETTINGS","gearshape")], id: \.0) { item in
                    Button { selection = item.0 } label: { HStack { Text(selection == item.0 ? "[" : " "); Image(systemName: item.2); Text(item.1).tracking(1); Spacer(); Text(selection == item.0 ? "]" : " ") }.frame(height: 30) }.buttonStyle(.plain).foregroundStyle(selection == item.0 ? GraveTheme.amber : GraveTheme.muted)
                }
                Spacer()
            }.padding(18).padding(.top, 22).frame(width: 210).background(GraveTheme.inset).overlay(alignment: .trailing) { Rectangle().fill(GraveTheme.ring).frame(width: 1) }
            VStack(spacing: 0) {
                HStack { Text(title).font(.system(size: 14, weight: .bold, design: .monospaced)).tracking(1.4).foregroundStyle(GraveTheme.ink); Spacer(); if selection == .terminal { GraveTargetPicker(graves: graves) }; if selection == .thisMac { Text(model.snapshot?.model ?? "SCANNING").font(.system(size: 10, design: .monospaced)).foregroundStyle(GraveTheme.muted) }; Button("↻ REFRESH") { model.refresh(); graves.refresh() }.buttonStyle(GraveButton()) }.padding(.horizontal, 22).frame(height: 54).background(GraveTheme.surface).overlay(alignment: .bottom) { Rectangle().fill(GraveTheme.ring).frame(height: 1) }
                Group {
                switch selection {
                case .graveyard: GraveyardView(graves: graves, selection: $selection)
                case .thisMac: SystemView(snapshot: model.snapshot)
                case .work: WorkView(model: model)
                case .network: NetworkView(model: model)
                case .terminal: TerminalDestination(graves: graves)
                case .settings: MacSettingsView(graves: graves, model: model, host: host)
                }
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            }.background(GraveTheme.page)
        }
        .task { model.refresh(); graves.refresh() }
        .onReceive(NotificationCenter.default.publisher(for: .openNativeTerminal)) { _ in selection = .terminal }
        .frame(minWidth: 900, minHeight: 600).graveRoot().background(GraveTheme.page.ignoresSafeArea())
    }
    private var title: String { switch selection { case .graveyard: "GRAVEYARD // TAILNET"; case .thisMac: "THIS MAC // SYSTEM"; case .work: "THIS MAC // WORK"; case .network: "THIS MAC // NETWORK"; case .terminal: "TERMINAL // \(graves.selected?.candidate.name.uppercased() ?? "REMOTE")"; case .settings: "SETTINGS" } }
}

struct MacWelcomeView: View {
    enum Choice { case connect, share }
    let choose: (Choice) -> Void

    var body: some View {
        ZStack {
            GraveTheme.page.ignoresSafeArea()
            VStack(alignment: .leading, spacing: 24) {
                HStack(spacing: 14) {
                    GraveMark(color: GraveTheme.ink, size: 46)
                    VStack(alignment: .leading, spacing: 5) {
                        Text("GRAVEDECAY // TAILNET")
                            .font(.system(size: 13, weight: .bold, design: .monospaced))
                            .tracking(1.4)
                        Text("NATIVE MACOS CLIENT")
                            .font(.system(size: 9, design: .monospaced))
                            .foregroundStyle(GraveTheme.muted)
                    }
                }
                Text("WHAT WILL THIS MAC DO FIRST?")
                    .font(.system(size: 20, weight: .bold, design: .monospaced))
                    .foregroundStyle(GraveTheme.ink)
                HStack(alignment: .top, spacing: 14) {
                    role("CONNECT TO GRAVES", "DISCOVER AND OPEN REMOTE GRAVES OVER TAILSCALE. THIS MAC WILL NOT HOST A SERVICE.", .connect)
                    role("SHARE THIS MAC", "START THIS MAC'S OPT-IN LOOPBACK PUBLISHER. FINISH TAILSCALE PUBLICATION IN SETTINGS.", .share)
                }
                Text("YOU CAN DO BOTH LATER.")
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundStyle(GraveTheme.amber)
            }
            .padding(34)
            .frame(maxWidth: 800, alignment: .leading)
        }
        .frame(minWidth: 900, minHeight: 600)
        .graveRoot()
    }

    private func role(_ title: String, _ detail: String, _ choice: Choice) -> some View {
        Button { choose(choice) } label: {
            VStack(alignment: .leading, spacing: 13) {
                Text(title).font(.system(size: 13, weight: .bold, design: .monospaced)).foregroundStyle(GraveTheme.ink)
                Text(detail).font(.system(size: 10, design: .monospaced)).foregroundStyle(GraveTheme.muted).lineSpacing(3)
                Text("[ SELECT ]").font(.system(size: 10, weight: .bold, design: .monospaced)).foregroundStyle(GraveTheme.amber)
            }
            .padding(18).frame(maxWidth: .infinity, minHeight: 142, alignment: .leading)
            .background(GraveTheme.surface).overlay(Rectangle().stroke(GraveTheme.ring))
        }.buttonStyle(.plain)
    }
}

private struct GraveTargetPicker: View {
    @ObservedObject var graves: GraveMenuModel
    var body: some View {
        Picker("TARGET", selection: $graves.selectedID) {
            ForEach(graves.graves) { grave in Text(grave.candidate.name.uppercased()).tag(Optional(grave.id)) }
        }.labelsHidden().pickerStyle(.menu).tint(GraveTheme.amber).font(.system(size: 10, weight: .bold, design: .monospaced))
            .accessibilityLabel("Target")
    }
}

private struct SystemView: View {
    let snapshot: MacSnapshot?
    var body: some View {
        ScrollView { VStack(alignment: .leading, spacing: 20) { Text("HOST VITALS").font(.system(size: 10, weight: .bold, design: .monospaced)).tracking(1.4).foregroundStyle(GraveTheme.amber)
            if let s = snapshot { LazyVGrid(columns: [GridItem(.adaptive(minimum: 210), spacing: 12)], spacing: 12) {
                MetricCard(title: "CPU TOTAL", value: s.cpu, detail: s.cpuDetail, percent: s.cpuPercent)
                MetricCard(title: "MEMORY PRESSURE", value: s.memory, detail: s.memoryDetail, percent: s.memoryPercent)
                MetricCard(title: "DISK", value: s.disk, detail: s.diskDetail, percent: s.diskPercent)
                MetricCard(title: "THERMAL", value: s.thermal, detail: s.thermalDetail, percent: nil)
                MetricCard(title: "BATTERY", value: s.battery, detail: s.batteryDetail, percent: s.batteryPercent, goodAtHigh: true)
                MetricCard(title: "SWAP", value: s.swap, detail: s.swapDetail, percent: s.swapPercent)
            }; GravePanel("identity") { HStack { Text(s.model).foregroundStyle(GraveTheme.ink); Spacer(); Text("macOS \(s.os) // up \(s.uptime)").foregroundStyle(GraveTheme.muted) } } }
            else { Text("[ sampling host metrics... ]").foregroundStyle(GraveTheme.muted) }
        }.frame(maxWidth: 980).padding(24) }.background(GraveTheme.page)
    }
}

private struct MetricCard: View {
    let title: String; let value: String; let detail: String; let percent: Double?; let goodAtHigh: Bool
    init(title: String, value: String, detail: String, percent: Double?, goodAtHigh: Bool = false) { self.title = title; self.value = value; self.detail = detail; self.percent = percent; self.goodAtHigh = goodAtHigh }
    var body: some View {
        VStack(alignment: .leading, spacing: 8) { Text(title).font(.system(size: 10, weight: .bold, design: .monospaced)).tracking(1).foregroundStyle(GraveTheme.muted); Text(value).font(.system(size: 23, weight: .bold, design: .monospaced)).foregroundStyle(GraveTheme.ink).shadow(color: GraveTheme.good.opacity(0.3), radius: 6); Text(detail).font(.system(size: 10, design: .monospaced)).foregroundStyle(GraveTheme.muted).lineLimit(2); GraveMeter(value: percent, goodAtHigh: goodAtHigh) }
            .frame(maxWidth: .infinity, minHeight: 106, alignment: .leading).padding(13).background(GraveTheme.surface).overlay(Rectangle().stroke(GraveTheme.ring))
    }
}

private struct WorkView: View {
    @ObservedObject var model: MacDashboardModel
    var body: some View {
        ScrollView { VStack(alignment: .leading, spacing: 18) { GravePanel("integrations") { HStack(spacing: 16) { IntegrationBadge(label: model.githubStatus, good: model.githubStatus.contains("authenticated")); IntegrationBadge(label: model.linearStatus, good: model.linearStatus.contains("assigned")); Spacer() } }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 360), spacing: 12)], spacing: 14) {
                ForEach(model.repositories) { repo in GravePanel(repo.name) { VStack(alignment: .leading, spacing: 6) {
                        HStack { StatusSquare(good: !repo.dirty); Text(repo.name.uppercased()).font(.system(size: 12, weight: .bold, design: .monospaced)).foregroundStyle(GraveTheme.ink); Spacer(); Text("[ \(repo.branch) ]").font(.system(size: 9, design: .monospaced)).foregroundStyle(GraveTheme.accentSoft) }
                        Text(repo.detail.uppercased()).font(.system(size: 9, design: .monospaced)).foregroundStyle(repo.dirty ? GraveTheme.amber : GraveTheme.muted)
                        Text(repo.lastCommit).font(.system(size: 10, design: .monospaced)).foregroundStyle(GraveTheme.ink2).lineLimit(1)
                        if let github = repo.github { Text(github.uppercased()).font(.system(size: 9, design: .monospaced)).foregroundStyle(GraveTheme.muted) }
                        if let url = repo.githubURL { WorkLink(prefix: "GH", title: "OPEN REPOSITORY", state: "REMOTE", url: url, good: true) }
                        ForEach(repo.pullRequests) { row in WorkLink(prefix: "PR #\(row.number)", title: row.title, state: row.state, url: row.url, good: row.state.lowercased() == "open") }
                        ForEach(repo.issues) { row in WorkLink(prefix: "ISSUE #\(row.number)", title: row.title, state: row.state, url: row.url, good: row.state.lowercased() == "open") }
                        if let ci = repo.latestCI { WorkLink(prefix: "CI #\(ci.number)", title: ci.title, state: ci.state, url: ci.url, good: ci.state.lowercased() == "success") }
                    }}
                }
            }; if model.repositories.isEmpty { Text("[ NO REPOSITORIES // CONFIGURE WORK ROOT ]").foregroundStyle(GraveTheme.muted) }
            if !model.linearIssues.isEmpty { GravePanel("linear // assigned") { VStack(alignment: .leading, spacing: 7) { ForEach(model.linearIssues) { issue in WorkLink(prefix: issue.identifier, title: issue.title, state: "ASSIGNED", url: issue.url, good: true) } } } }
        }.frame(maxWidth: 980).padding(24) }.background(GraveTheme.page)
    }
}

private struct IntegrationBadge: View {
    let label: String; let good: Bool
    var body: some View { HStack(spacing: 6) { StatusSquare(good: good); Text(label.uppercased()).lineLimit(1) }.font(.system(size: 9, weight: .medium, design: .monospaced)).foregroundStyle(good ? GraveTheme.ink2 : GraveTheme.muted).padding(.vertical, 5).padding(.horizontal, 7).background(GraveTheme.inset).overlay(Rectangle().stroke(GraveTheme.hairline)) }
}

private struct WorkLink: View {
    let prefix, title, state: String; let url: URL; let good: Bool
    var body: some View { Link(destination: url) { HStack(spacing: 7) { StatusSquare(good: good); Text(prefix).foregroundStyle(GraveTheme.amber); Text(title).foregroundStyle(GraveTheme.ink2).lineLimit(1); Spacer(); Text(state.uppercased()).foregroundStyle(GraveTheme.muted) }.font(.system(size: 9, design: .monospaced)).padding(.vertical, 2) }.buttonStyle(.plain) }
}

private struct NetworkView: View {
    @ObservedObject var model: MacDashboardModel
    var body: some View {
        ScrollView { VStack(alignment: .leading, spacing: 18) { GravePanel("tailscale topology") { HStack { StatusSquare(good: model.tailnetStatus == "Running"); Text(model.tailnetStatus.uppercased()).foregroundStyle(GraveTheme.ink2); Spacer(); Text(model.tailnetName).foregroundStyle(GraveTheme.muted) }.font(.system(size: 10, design: .monospaced)) }
            Text("ACTIVE INTERFACE TOTALS").font(.system(size: 10, weight: .bold, design: .monospaced)).tracking(1.2).foregroundStyle(GraveTheme.amber)
            if model.networkInterfaces.isEmpty { ThemedEmpty(title: "NO ACTIVE INTERFACES", detail: "BYTE TOTALS ARE CURRENTLY UNAVAILABLE") }
            else { LazyVGrid(columns: [GridItem(.adaptive(minimum: 260), spacing: 12)], spacing: 12) { ForEach(model.networkInterfaces) { InterfaceCard(interface: $0) } } }
            Text("READ-ONLY // TAILSCALE STATE IS NEVER CHANGED // COUNTERS ARE TOTALS").font(.system(size: 9, design: .monospaced)).foregroundStyle(GraveTheme.muted)
        }.frame(maxWidth: 980).padding(24) }.background(GraveTheme.page)
    }
}

private struct InterfaceCard: View {
    let interface: NetworkInterface
    var total: Double { Double(max(1, interface.received + interface.sent)) }
    var body: some View { GravePanel(interface.name) { VStack(alignment: .leading, spacing: 10) {
        HStack { VStack(alignment: .leading, spacing: 3) { Text("RX TOTAL").foregroundStyle(GraveTheme.muted); Text(formatBytes(interface.received)).foregroundStyle(GraveTheme.good) }; Spacer(); VStack(alignment: .trailing, spacing: 3) { Text("TX TOTAL").foregroundStyle(GraveTheme.muted); Text(formatBytes(interface.sent)).foregroundStyle(GraveTheme.amber) } }.font(.system(size: 11, weight: .medium, design: .monospaced))
        GeometryReader { proxy in HStack(spacing: 2) { Rectangle().fill(GraveTheme.good).frame(width: max(2, proxy.size.width * CGFloat(Double(interface.received) / total))); Rectangle().fill(GraveTheme.amber) } }.frame(height: 5).background(GraveTheme.hairline)
    } } }
}

private struct GraveyardView: View {
    @ObservedObject var graves: GraveMenuModel
    @Binding var selection: MacNativeContentView.Section
    @State private var showingAll = false
    var body: some View {
        if !showingAll, let grave = graves.selected {
            GraveDetailView(grave: grave, graves: graves, selection: $selection) { showingAll = true }
        } else {
        ScrollView { LazyVGrid(columns: [GridItem(.adaptive(minimum: 330), spacing: 12)], spacing: 14) {
            if graves.graves.isEmpty { GravePanel("status") { TailscaleOnboardingView(model: graves) } }
            ForEach(graves.graves) { grave in
                GravePanel(grave.candidate.name) { VStack(alignment: .leading, spacing: 7) {
                    HStack { StatusSquare(good: grave.reachable); Image(systemName: icon(for: GravePresentation.condition(summary: grave.summary, reachable: grave.reachable))); Text(grave.candidate.name.uppercased()).fontWeight(.bold); if graves.selectedID == grave.id { Text("[ SELECTED ]").foregroundStyle(GraveTheme.amber) }; Spacer(); Text(grave.reachable ? "ONLINE" : "UNREACHABLE").foregroundStyle(grave.reachable ? GraveTheme.good : GraveTheme.muted) }.font(.system(size: 10, design: .monospaced)).foregroundStyle(GraveTheme.ink)
                    if let summary = grave.summary { Text("\(summary.node.platform.uppercased()) // CPU \(GravePresentation.percent(summary.resources.cpu_pct))").font(.system(size: 9, design: .monospaced)).foregroundStyle(GraveTheme.ink2) }
                    if let s = grave.summary { VStack(alignment: .leading, spacing: 5) { HStack { Text("MEM \(GravePresentation.percent(s.resources.memory_pct))"); Text("DISK \(GravePresentation.percent(s.resources.disk_pct))"); Spacer(); Text("UP \(GravePresentation.uptime(s.node.uptime_s))") }.foregroundStyle(GraveTheme.muted); HStack { Text("SESSIONS \(s.activity.sessions_live)"); Text("PROBLEMS \(s.problems)").foregroundStyle(s.problems > 0 ? GraveTheme.crit : GraveTheme.good) } }.font(.system(size: 9, design: .monospaced)) }
                    HStack { CapabilityButton(title: "T3", host: grave.candidate.dns, path: grave.summary?.capabilities.t3); if grave.summary?.capabilities.terminal != nil { Button("TERMINAL") { graves.select(grave); selection = .terminal }.buttonStyle(GraveButton()) } }
                }}.contentShape(Rectangle()).onTapGesture { graves.select(grave); showingAll = false }
            }
        }.frame(maxWidth: 980).padding(24) }.background(GraveTheme.page)
        }
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

private struct GraveDetailView: View {
    let grave: GraveMenuModel.Grave
    @ObservedObject var graves: GraveMenuModel
    @Binding var selection: MacNativeContentView.Section
    let back: () -> Void
    var body: some View {
        ScrollView { VStack(alignment: .leading, spacing: 20) {
            Button("← ALL GRAVES") { back() }.buttonStyle(GraveButton())
            GravePanel(grave.candidate.name) { VStack(alignment: .leading, spacing: 8) {
                HStack { StatusSquare(good: grave.reachable); Text(grave.reachable ? "ONLINE" : "UNREACHABLE").foregroundStyle(grave.reachable ? GraveTheme.good : GraveTheme.crit); Spacer(); Text("SEEN \(GravePresentation.age(grave.summary?.observed_at))").foregroundStyle(GraveTheme.muted) }
                Text(grave.summary.map { "\($0.node.host.uppercased()) // \($0.node.platform.uppercased()) // \($0.node.mode.uppercased())" } ?? "NO VALIDATED SUMMARY AVAILABLE").foregroundStyle(GraveTheme.ink2)
            }.font(.system(size: 10, design: .monospaced)) }
            if let summary = grave.summary {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 210), spacing: 12)], spacing: 12) {
                    MetricCard(title: "CPU", value: GravePresentation.percent(summary.resources.cpu_pct), detail: "PROCESSOR LOAD", percent: summary.resources.cpu_pct)
                    MetricCard(title: "MEMORY", value: GravePresentation.percent(summary.resources.memory_pct), detail: "MEMORY IN USE", percent: summary.resources.memory_pct)
                    MetricCard(title: "DISK", value: GravePresentation.percent(summary.resources.disk_pct), detail: "DISK IN USE", percent: summary.resources.disk_pct)
                    MetricCard(title: "TEMPERATURES", value: "CPU \(GravePresentation.temperature(summary.resources.cpu_temp_c))", detail: "GPU \(GravePresentation.temperature(summary.resources.gpu_temp_c))", percent: nil)
                }
                GravePanel("activity // health") { VStack(alignment: .leading, spacing: 7) {
                    HStack { Text("UP \(GravePresentation.uptime(summary.node.uptime_s))"); Spacer(); Text("\(summary.problems) PROBLEMS").foregroundStyle(summary.problems > 0 ? GraveTheme.crit : GraveTheme.good) }
                    HStack { Text("SESSIONS \(summary.activity.sessions_live) LIVE"); Text("\(summary.activity.sessions_frozen) FROZEN"); Spacer(); Text("SERVICES \(summary.health.services_failed) FAILED"); Text("CONTAINERS \(summary.health.containers_problem) PROBLEM") }
                }.font(.system(size: 10, design: .monospaced)).foregroundStyle(GraveTheme.ink2) }
                HStack { CapabilityButton(title: "T3", host: grave.candidate.dns, path: summary.capabilities.t3); if summary.capabilities.terminal != nil { Button("TERMINAL") { graves.select(grave); selection = .terminal }.buttonStyle(GraveButton()) } }
            }
        }.frame(maxWidth: 980).padding(24) }.background(GraveTheme.page)
    }
}

struct TailscaleOnboardingView: View {
    @ObservedObject var model: GraveMenuModel

    private var title: String {
        switch model.state {
        case .ready: return "TAILSCALE CONNECTED"
        case .missingTailscale: return "TAILSCALE NOT INSTALLED"
        case .loggedOut: return "TAILSCALE ACTION REQUIRED"
        case .noAppliances: return model.tailscaleUnavailable ? "TAILSCALE UNAVAILABLE" : "NO GRAVES"
        case .scanning: return "SCANNING TAILNET"
        default: return "WAITING FOR TAILSCALE"
        }
    }

    private var detail: String {
        switch model.state {
        case .ready: return "\(model.graves.count) GRAVE\(model.graves.count == 1 ? "" : "S") FOUND ON THIS TAILNET."
        case .missingTailscale: return "INSTALL THE OFFICIAL TAILSCALE APP TO DISCOVER GRAVES."
        case .loggedOut: return "OPEN TAILSCALE, CONNECT OR SIGN IN, THEN REFRESH."
        case .noAppliances: return model.tailscaleUnavailable ? "TAILSCALE IS INSTALLED BUT STATUS IS UNAVAILABLE. CHECK THE APP, THEN REFRESH." : "NO REACHABLE GRAVES FOUND. CHECK TAILSCALE, THEN REFRESH."
        case .scanning: return "DISCOVERING TAILNET GRAVES…"
        default: return "REFRESH TO DISCOVER TAILNET GRAVES."
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack { StatusSquare(good: false); Text(title).foregroundStyle(GraveTheme.ink) }
            Text(detail).foregroundStyle(GraveTheme.muted)
            HStack {
                if model.state == .missingTailscale { Button("GET TAILSCALE") { model.getTailscale() }.buttonStyle(GraveButton()) }
                if model.state == .loggedOut, model.canOpenTailscale { Button("OPEN TAILSCALE") { model.openTailscale() }.buttonStyle(GraveButton()) }
                Button("↻ REFRESH") { model.refresh() }.buttonStyle(GraveButton()).disabled(model.state == .scanning)
            }
        }.font(.system(size: 10, design: .monospaced)).frame(maxWidth: .infinity, minHeight: 76, alignment: .leading)
    }
}

private struct TerminalDestination: View {
    @ObservedObject var graves: GraveMenuModel
    @StateObject private var status = TerminalStatus()
    var body: some View {
        if let grave = graves.selected, let box = BoxConfig(host: grave.candidate.dns, terminalPath: grave.summary?.capabilities.terminal) { VStack(spacing: 0) {
            HStack { StatusSquare(good: status.state == .connected); VStack(alignment: .leading, spacing: 2) { Text("\(status.state.rawValue) // \(grave.candidate.name.uppercased())"); if status.lastCause != "NONE" && status.lastCause != "REMOTE CLOSED" { Text(status.lastCause).foregroundStyle(GraveTheme.crit) } }; Spacer(); Button("RETRY") { status.retry() }.buttonStyle(GraveButton()); Button("COPY DIAGNOSTICS") { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(status.diagnostics, forType: .string) }.buttonStyle(GraveButton()) }.font(.system(size: 10, design: .monospaced)).padding(10).background(GraveTheme.surface)
            TerminalPane(box: box, urlSession: .shared, status: status).id("\(grave.id)-\(status.retryID)").padding(1).background(GraveTheme.ring)
        }.padding(20).background(GraveTheme.page) }
        else if graves.selected != nil { ThemedEmpty(title: "TERMINAL NOT PUBLISHED BY THIS GRAVE", detail: "SELECT A GRAVE THAT ADVERTISES A SAFE TERMINAL LINK") .padding(24).frame(maxWidth: .infinity, maxHeight: .infinity).background(GraveTheme.page) }
        else { ThemedEmpty(title: "CHOOSE A GRAVE", detail: "SELECT A GRAVE IN GRAVEYARD AFTER TAILSCALE DISCOVERY") .padding(24).frame(maxWidth: .infinity, maxHeight: .infinity).background(GraveTheme.page) }
    }
}

struct MacSettingsView: View {
    @ObservedObject var graves: GraveMenuModel
    @ObservedObject var model: MacDashboardModel
    @ObservedObject var host: MacNativeHost
    @AppStorage("macWelcomeCompleted") private var macWelcomeCompleted = false
    @State private var root = ""; @State private var linearKey = ""
    var body: some View {
        ScrollView { VStack(spacing: 20) {
            GravePanel("first action") { HStack { Text("CONNECT AND SHARE CAN BOTH BE USED.").foregroundStyle(GraveTheme.muted); Spacer(); Button("CHOOSE ROLE AGAIN") { macWelcomeCompleted = false }.buttonStyle(GraveButton()) } }
            GravePanel("tailnet access") { TailscaleOnboardingView(model: graves) }
            GravePanel("work root") { VStack(alignment: .leading, spacing: 9) { Text("LOCAL DIRECTORY SCANNED FOR GIT REPOSITORIES").foregroundStyle(GraveTheme.muted); TextField("Repository folder", text: $root).textFieldStyle(.plain).padding(8).background(GraveTheme.inset).overlay(Rectangle().stroke(GraveTheme.hairline)); Button("SAVE ROOT") { model.setWorkRoot(root) }.buttonStyle(GraveButton()); if let status = model.workRootStatus { Text(status).foregroundStyle(GraveTheme.crit) } } }
            GravePanel("linear // read-only") { VStack(alignment: .leading, spacing: 9) { Text("THE TOKEN STAYS IN THIS MAC'S KEYCHAIN").foregroundStyle(GraveTheme.muted); SecureField("lin_api_…", text: $linearKey).textFieldStyle(.plain).padding(8).background(GraveTheme.inset).overlay(Rectangle().stroke(GraveTheme.hairline)); HStack { Button("SAVE KEY") { model.saveLinearKey(linearKey); linearKey = "" }; Button("REMOVE KEY") { model.removeLinearKey() } }.buttonStyle(GraveButton()); Text(model.keychainStatus).foregroundStyle(GraveTheme.muted) } }
            GravePanel("local host") { VStack(alignment: .leading, spacing: 9) { Text(host.detail).foregroundStyle(host.state == .hosted ? GraveTheme.good : GraveTheme.muted); HStack { Button(host.state == .existingCompanion ? "RETRY LOCAL HOST" : "START LOCAL HOST") { host.enable() }.buttonStyle(GraveButton()).disabled(host.state == .hosted || host.state == .starting); Button(host.state == .hosted ? "STOP LOCAL HOST" : "CANCEL HOST REQUEST") { host.disable() }.buttonStyle(GraveButton()).disabled(host.state != .hosted && !host.hostRequested); if host.state == .hosted { Button("COPY TAILSCALE PUBLISH COMMAND") { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(host.manualServeCommand, forType: .string) }.buttonStyle(GraveButton()) } }; if host.state == .existingCompanion { Text("NATIVE HOSTING WAS NOT STARTED: LEGACY COMPANION OWNS 4712.").foregroundStyle(GraveTheme.crit) }; Text("DOES NOT CHANGE TAILSCALE. HOSTING STOPS WHEN THIS APP QUITS; ENABLE LAUNCH AT LOGIN TO KEEP THE APP AVAILABLE.").foregroundStyle(GraveTheme.muted) } }
            GravePanel("startup") { Toggle("LAUNCH AT LOGIN", isOn: Binding(get: { model.launchAtLogin }, set: { model.setLaunchAtLogin($0) })).toggleStyle(.switch).tint(GraveTheme.good); if let error = model.launchAtLoginError { Text(error).foregroundStyle(GraveTheme.crit) } }
            GravePanel("about") { Text("NATIVE REMOTE DASHBOARDS // T3 OPENS IN YOUR BROWSER").foregroundStyle(GraveTheme.muted) }
        }.font(.system(size: 10, design: .monospaced)).frame(maxWidth: 760).padding(24) }.background(GraveTheme.page).onAppear { root = model.workRoot }
    }
}

private struct CapabilityButton: View {
    let title: String; let host: String; let path: String
    init?(title: String, host: String, path: String?) { guard let path else { return nil }; self.title = title; self.host = host; self.path = path }
    var body: some View { Button(title) { if let url = GravePresentation.link(host: host, path: path) { NSWorkspace.shared.open(url) } }.buttonStyle(GraveButton()) }
}

private struct StatusSquare: View { let good: Bool; var body: some View { Rectangle().fill(good ? GraveTheme.good : GraveTheme.crit).frame(width: 8, height: 8).shadow(color: good ? GraveTheme.good : GraveTheme.crit, radius: 4) } }

private struct ThemedEmpty: View {
    let title, detail: String
    var body: some View { GravePanel("status") { VStack(alignment: .leading, spacing: 7) { HStack { StatusSquare(good: false); Text(title).foregroundStyle(GraveTheme.ink) }; Text(detail).foregroundStyle(GraveTheme.muted) }.font(.system(size: 10, design: .monospaced)).frame(maxWidth: .infinity, minHeight: 76, alignment: .leading) } }
}

private func formatBytes(_ bytes: Int64) -> String { ByteCountFormatter.string(fromByteCount: bytes, countStyle: .decimal).uppercased() }

struct MacRepository: Identifiable { let path, name, branch, detail, lastCommit: String; let dirty: Bool; let github: String?; let githubURL: URL?; let pullRequests, issues: [GitHubRow]; let latestCI: GitHubRow?; var id: String { path } }
struct LinearIssue: Identifiable { let identifier, title: String; let url: URL; var id: String { identifier } }
struct NetworkInterface: Identifiable { let name: String; let received, sent: Int64; var id: String { name } }
struct MacSnapshot { let model, os, uptime, cpu, memory, disk, thermal, battery, swap: String; let cpuDetail, memoryDetail, diskDetail, thermalDetail, batteryDetail, swapDetail: String; let cpuPercent, memoryPercent, diskPercent, batteryPercent, swapPercent: Double? }

@MainActor
final class MacDashboardModel: ObservableObject {
    @Published private(set) var snapshot: MacSnapshot?
    @Published private(set) var repositories: [MacRepository] = []
    @Published private(set) var tailnetStatus = "Checking…"
    @Published private(set) var tailnetName = "—"
    @Published private(set) var githubStatus = "Checking GitHub CLI…"
    @Published private(set) var linearStatus = "Linear not configured"
    @Published private(set) var linearIssues: [LinearIssue] = []
    @Published private(set) var networkInterfaces: [NetworkInterface] = []
    @Published private(set) var workRoot: String
    @Published private(set) var keychainStatus = "Stored in this Mac’s Keychain. Assigned issues are read-only."
    @Published private(set) var workRootStatus: String?
    @Published var launchAtLogin = SMAppService.mainApp.status != .notRegistered
    @Published private(set) var launchAtLoginError: String?
    private let defaults = UserDefaults.standard
    private var refreshTask: Task<Void, Never>?
    private weak var nativeHost: MacNativeHost?
    init() { workRoot = UserDefaults.standard.string(forKey: "macWorkRoot") ?? (FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Sites").path) }
    func refresh() {
        refreshTask?.cancel()
        let root = workRoot, key = Keychain.value(account: "linear-api-key"), deadline = Date().addingTimeInterval(8)
        refreshTask = Task.detached(priority: .utility) { [weak self] in
            guard let result = MacCollector.collect(root: root, linearKey: key, deadline: deadline), !Task.isCancelled else { return }
            await MainActor.run { guard !Task.isCancelled else { return }; self?.apply(result) }
        }
    }
    func setWorkRoot(_ value: String) { let url = URL(fileURLWithPath: (value as NSString).expandingTildeInPath); var directory: ObjCBool = false; guard FileManager.default.fileExists(atPath: url.path, isDirectory: &directory), directory.boolValue else { workRootStatus = "Repository folder must be an existing directory."; return }; workRootStatus = nil; workRoot = url.path; defaults.set(workRoot, forKey: "macWorkRoot"); refresh() }
    func saveLinearKey(_ key: String) { guard !key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }; keychainStatus = Keychain.set(key, account: "linear-api-key") ? "Stored in this Mac’s Keychain." : "Could not save the Linear key to Keychain."; refresh() }
    func removeLinearKey() { keychainStatus = Keychain.remove(account: "linear-api-key") ? "Linear key removed." : "Could not remove the Linear key from Keychain."; refresh() }
    func setLaunchAtLogin(_ enabled: Bool) {
        do { if enabled { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }; launchAtLogin = SMAppService.mainApp.status != .notRegistered; launchAtLoginError = SMAppService.mainApp.status == .requiresApproval ? "Launch at Login needs approval in System Settings." : nil }
        catch { launchAtLogin = SMAppService.mainApp.status != .notRegistered; launchAtLoginError = "Launch at Login: \(error.localizedDescription)" }
    }
    func setNativeHost(_ host: MacNativeHost) { nativeHost = host; host.update(snapshot: snapshot) }
    private func apply(_ result: CollectionResult) { snapshot = result.snapshot; nativeHost?.update(snapshot: result.snapshot); repositories = result.repositories; tailnetStatus = result.tailnetStatus; tailnetName = result.tailnetName; githubStatus = result.githubStatus; linearStatus = result.linearStatus; linearIssues = result.linearIssues; networkInterfaces = result.networkInterfaces }
}

private struct CollectionResult { let snapshot: MacSnapshot; let repositories: [MacRepository]; let tailnetStatus, tailnetName, githubStatus, linearStatus: String; let linearIssues: [LinearIssue]; let networkInterfaces: [NetworkInterface] }
private enum MacCollector {
    @TaskLocal static var activeDeadline: Date?
    static func collect(root: String, linearKey: String?, deadline: Date) -> CollectionResult? {
        Self.$activeDeadline.withValue(deadline) { collectBounded(root: root, linearKey: linearKey, deadline: deadline) }
    }
    private static func collectBounded(root: String, linearKey: String?, deadline: Date) -> CollectionResult? {
        guard !Task.isCancelled, Date() < deadline else { return nil }
        let os = ProcessInfo.processInfo.operatingSystemVersionString.replacingOccurrences(of: "Version ", with: "")
        let disk = (try? URL(fileURLWithPath: NSHomeDirectory()).resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey, .volumeTotalCapacityKey]))
        let total = Int64(disk?.volumeTotalCapacity ?? 0), available = disk?.volumeAvailableCapacityForImportantUsage ?? 0
        let used: String; let diskPercent: Double?
        if total > 0 {
            let percent = Int((Double(total - available) / Double(total)) * 100)
            used = "\(percent)%"; diskPercent = Double(percent)
        } else { used = "—"; diskPercent = nil }
        let memoryStats = NativeParsers.memoryActivity(pressure: command("/usr/bin/memory_pressure", ["-Q"]) ?? "", vmStat: command("/usr/bin/vm_stat", []) ?? "")
        let cpuStats = NativeParsers.cpuActivity(command("/usr/bin/top", ["-l", "1", "-n", "0"]) ?? "")
        let uptime = GravePresentation.uptime(ProcessInfo.processInfo.systemUptime)
        let batteryRaw = command("/usr/bin/pmset", ["-g", "batt"]) ?? ""; let batteryPercent = NativeParsers.batteryPercent(batteryRaw)
        let thermalStats = NativeParsers.thermalActivity(command("/usr/bin/pmset", ["-g", "therm"]) ?? "")
        let thermalAvailable = thermalStats.speedLimit != nil || thermalStats.availableCPUs != nil
        let swapStats = NativeParsers.swapActivity(command("/usr/sbin/sysctl", ["-n", "vm.swapusage"]) ?? "")
        let load = command("/usr/sbin/sysctl", ["-n", "vm.loadavg"])?.replacingOccurrences(of: "{", with: "").replacingOccurrences(of: "}", with: "") ?? "—"
        let installed = ByteCountFormatter.string(fromByteCount: Int64(ProcessInfo.processInfo.physicalMemory), countStyle: .memory)
        let cpuDetail = cpuStats.map { "USER \(String(format: "%.1f", $0.user))% // SYS \(String(format: "%.1f", $0.system))% // LOAD \(load) // \(ProcessInfo.processInfo.processorCount) CORES" } ?? "CPU SAMPLE UNAVAILABLE // LOAD \(load) // \(ProcessInfo.processInfo.processorCount) CORES"
        let compressed = memoryStats.compressedBytes.map { formatBytes($0) } ?? "UNAVAILABLE"
        let thermalLimits = [thermalStats.speedLimit.map { "CPU LIMIT \($0)%" }, thermalStats.availableCPUs.map { "\($0) AVAILABLE" }].compactMap { $0 }.joined(separator: " // ")
        let snapshot = MacSnapshot(
            model: command("/usr/sbin/sysctl", ["-n", "hw.model"]) ?? "Mac", os: os, uptime: uptime,
            cpu: cpuStats.map { String(format: "%.0f%%", $0.total) } ?? "—",
            memory: memoryStats.usedPercent.map { String(format: "%.0f%%", $0) } ?? "—", disk: used,
            thermal: thermalAvailable ? (thermalStats.throttled ? "THROTTLED" : "NOMINAL") : "UNAVAILABLE",
            battery: batteryPercent.map { String(format: "%.0f%%", $0) } ?? "N/A",
            swap: swapStats.map { String(format: "%.0f%%", $0.usedPercent) } ?? "—",
            cpuDetail: cpuDetail, memoryDetail: "\(installed.uppercased()) INSTALLED // \(compressed) COMPRESSED",
            diskDetail: "\(formatBytes(total - available)) USED",
            thermalDetail: thermalLimits.isEmpty ? "LIMITS UNAVAILABLE" : thermalLimits,
            batteryDetail: batteryPercent == nil ? "UNAVAILABLE" : batteryRaw.contains("charging") ? "CHARGING" : batteryRaw.contains("AC Power") ? "AC POWER" : "DISCHARGING",
            swapDetail: swapStats.map { "\(formatBytes($0.usedBytes)) USED // \(formatBytes($0.totalBytes)) TOTAL" } ?? "UNAVAILABLE",
            cpuPercent: cpuStats?.total, memoryPercent: memoryStats.usedPercent, diskPercent: diskPercent, batteryPercent: batteryPercent, swapPercent: swapStats?.usedPercent
        )
        let status = command("/usr/local/bin/tailscale", ["status", "--json"], environment: ["TAILSCALE_BE_CLI": "1"]) ?? command("/Applications/Tailscale.app/Contents/MacOS/Tailscale", ["status", "--json"], environment: ["TAILSCALE_BE_CLI": "1"])
        let tailnet = status.flatMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
        let backend = tailnet?["BackendState"] as? String ?? "Not installed or signed out"
        let name = ((tailnet?["Self"] as? [String: Any])?["DNSName"] as? String) ?? "—"
        let gh = command("/opt/homebrew/bin/gh", ["auth", "status"]) ?? command("/usr/local/bin/gh", ["auth", "status"])
        let github = gh == nil ? "GitHub CLI not installed or not signed in" : "GitHub CLI authenticated (read-only PR/CI status)"
        let repos = repositories(at: root, ghAvailable: gh != nil, deadline: deadline)
        guard !Task.isCancelled, Date() < deadline else { return nil }
        let linear = linearIssues(key: linearKey)
        let network = NativeParsers.interfaceBytes(command("/usr/sbin/netstat", ["-ibn"]) ?? "").prefix(4).map { NetworkInterface(name: $0.0, received: $0.1, sent: $0.2) }
        return CollectionResult(snapshot: snapshot, repositories: repos, tailnetStatus: backend, tailnetName: name, githubStatus: github, linearStatus: linear.0, linearIssues: linear.1, networkInterfaces: Array(network))
    }
    static func repositories(at root: String, ghAvailable: Bool, deadline: Date) -> [MacRepository] {
        guard let enumerator = FileManager.default.enumerator(at: URL(fileURLWithPath: root), includingPropertiesForKeys: [.isDirectoryKey], options: [.skipsPackageDescendants]) else { return [] }
        var found = [String](); let rootDepth = URL(fileURLWithPath: root).pathComponents.count; var entries = 0
        while let url = enumerator.nextObject() as? URL, found.count < 8, entries < 2_000, Date() < deadline, !Task.isCancelled {
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
            let enriched = ghAvailable && index < 4 && !Task.isCancelled && Date() < deadline
            let github = GitHubRemote.repository(remote).map { _ in "GitHub remote" }
            let repo = GitHubRemote.repository(remote)
            let pulls = enriched ? (repo.map { githubRows($0, kind: "pr") } ?? []) : []
            let issues = enriched ? (repo.map { githubRows($0, kind: "issue") } ?? []) : []
            let ci = enriched ? repo.flatMap { githubRun($0) } : nil
            let lastCommit = command("/usr/bin/git", ["-C", path, "log", "-1", "--format=%h %s"]) ?? "Last commit unavailable"
            return MacRepository(path: path, name: URL(fileURLWithPath: path).lastPathComponent, branch: branch, detail: detail, lastCommit: lastCommit, dirty: dirty, github: github, githubURL: GitHubRemote.url(remote), pullRequests: pulls, issues: issues, latestCI: ci)
        }
    }
    static func githubSummary(_ remote: String?, enabled: Bool) -> String? {
        guard let repo = GitHubRemote.repository(remote) else { return nil }
        guard enabled else { return "GitHub remote" }
        let output = command("/opt/homebrew/bin/gh", ["pr", "list", "--repo", repo, "--limit", "3", "--json", "number,statusCheckRollup"]) ?? command("/usr/local/bin/gh", ["pr", "list", "--repo", repo, "--limit", "3", "--json", "number,statusCheckRollup"])
        guard let output, let prs = try? JSONSerialization.jsonObject(with: Data(output.utf8)) as? [[String: Any]] else { return "GitHub unavailable" }
        let failing = prs.contains { (($0["statusCheckRollup"] as? [[String: Any]]) ?? []).contains { ["FAILURE", "ERROR"].contains($0["conclusion"] as? String ?? "") } }
        return "GitHub: \(prs.count) open PR(s)\(failing ? ", CI failing" : "")"
    }
    static func githubRows(_ repo: String, kind: String) -> [GitHubRow] {
        let args = [kind, "list", "--repo", repo, "--limit", "3", "--json", "number,title,state,url"]
        let output = command("/opt/homebrew/bin/gh", args) ?? command("/usr/local/bin/gh", args)
        return output.map { NativeParsers.githubRows(Data($0.utf8)) } ?? []
    }
    static func githubRun(_ repo: String) -> GitHubRow? {
        let args = ["run", "list", "--repo", repo, "--limit", "1", "--json", "databaseId,displayTitle,workflowName,status,conclusion,url"]
        let output = command("/opt/homebrew/bin/gh", args) ?? command("/usr/local/bin/gh", args)
        return output.flatMap { NativeParsers.githubRun(Data($0.utf8)) }
    }
    static func linearIssues(key: String?) -> (String, [LinearIssue]) {
        guard let key, !key.isEmpty else { return ("Linear not configured — add a key in Settings", []) }
        // This first native release preserves the read-only contract; the query stays bounded.
        let body = #"{"query":"query { viewer { assignedIssues(first: 10) { nodes { identifier title url } } } }"}"#
        guard let url = URL(string: "https://api.linear.app/graphql"), var request = Optional(URLRequest(url: url)) else { return ("Linear unavailable", []) }
        request.httpMethod = "POST"; request.httpBody = Data(body.utf8); request.setValue(key, forHTTPHeaderField: "Authorization"); request.setValue("application/json", forHTTPHeaderField: "Content-Type"); request.timeoutInterval = 3
        guard let responseData = BoundedHTTP.post(request, deadline: activeDeadline ?? Date()),
              let root = try? JSONSerialization.jsonObject(with: responseData) as? [String: Any],
              let nodes = (((root["data"] as? [String: Any])?["viewer"] as? [String: Any])?["assignedIssues"] as? [String: Any])?["nodes"] as? [[String: Any]] else { return ("Linear configured, but unavailable", []) }
        return ("Linear assigned to me (read-only)", nodes.compactMap { guard let id = $0["identifier"] as? String, let title = $0["title"] as? String, let raw = $0["url"] as? String, let url = URL(string: raw), url.scheme == "https", url.host == "linear.app" else { return nil }; return LinearIssue(identifier: id, title: title, url: url) })
    }
    static func command(_ executable: String, _ arguments: [String], environment: [String: String] = [:]) -> String? {
        let deadline = activeDeadline ?? Date().addingTimeInterval(2)
        guard !Task.isCancelled, Date() < deadline else { return nil }
        guard FileManager.default.isExecutableFile(atPath: executable) else { return nil }
        let process = Process(); process.executableURL = URL(fileURLWithPath: executable); process.arguments = arguments; process.environment = ProcessInfo.processInfo.environment.merging(environment) { _, new in new }
        let pipe = Pipe(); process.standardOutput = pipe; process.standardError = FileHandle.nullDevice
        let lock = NSLock(); var output = Data(); var oversized = false
        let done = DispatchSemaphore(value: 0)
        let eof = DispatchSemaphore(value: 0)
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let chunk = handle.availableData; guard !chunk.isEmpty else { eof.signal(); return }
            lock.lock(); output.append(chunk); oversized = output.count > 65_536; lock.unlock()
            if oversized { process.terminate() }
        }
        process.terminationHandler = { _ in done.signal() }
        do { try process.run() } catch { return nil }
        while done.wait(timeout: .now() + 0.05) == .timedOut, !Task.isCancelled, Date() < deadline {}
        if process.isRunning {
            process.terminate()
            if done.wait(timeout: .now() + 0.2) == .timedOut { kill(process.processIdentifier, SIGKILL); _ = done.wait(timeout: .now() + 1) }
        }
        _ = eof.wait(timeout: .now() + min(0.05, max(0, deadline.timeIntervalSinceNow)))
        pipe.fileHandleForReading.readabilityHandler = nil
        guard !process.isRunning else { pipe.fileHandleForReading.closeFile(); return nil }
        lock.lock(); let data = output; let valid = !oversized && data.count <= 65_536; lock.unlock()
        pipe.fileHandleForReading.closeFile()
        guard process.terminationStatus == 0, valid else { return nil }
        return String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

private final class BoundedHTTP: NSObject, URLSessionDataDelegate, URLSessionTaskDelegate {
    private var data = Data(); private var valid = false; private let done = DispatchSemaphore(value: 0)
    static func post(_ request: URLRequest, deadline: Date) -> Data? {
        let collector = BoundedHTTP(); let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 3; config.timeoutIntervalForResource = 3
        let session = URLSession(configuration: config, delegate: collector, delegateQueue: nil)
        session.dataTask(with: request).resume()
        guard !Task.isCancelled, deadline > Date(), collector.done.wait(timeout: .now() + deadline.timeIntervalSinceNow) == .success, collector.valid, collector.data.count <= 65_536 else { session.invalidateAndCancel(); return nil }
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
    static func set(_ value: String, account: String) -> Bool { let base: [CFString: Any] = [kSecClass: kSecClassGenericPassword, kSecAttrService: "com.projectmushroom.gravedecay", kSecAttrAccount: account]; let update = [kSecValueData: Data(value.utf8)] as CFDictionary; let status = SecItemUpdate(base as CFDictionary, update); if status == errSecSuccess { return true }; guard status == errSecItemNotFound else { return false }; var query = base; query[kSecValueData] = Data(value.utf8); return SecItemAdd(query as CFDictionary, nil) == errSecSuccess }
    static func remove(account: String) -> Bool { let query: [CFString: Any] = [kSecClass: kSecClassGenericPassword, kSecAttrService: "com.projectmushroom.gravedecay", kSecAttrAccount: account]; let status = SecItemDelete(query as CFDictionary); return status == errSecSuccess || status == errSecItemNotFound }
}
#endif
