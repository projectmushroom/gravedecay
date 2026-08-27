import SwiftUI
import AppKit
import GravedecayKit

@main
struct GravedecayMenuBarApp: App {
    @StateObject private var fleet = FleetModel()

    init() { NSApplication.shared.setActivationPolicy(.accessory) }

    var body: some Scene {
        MenuBarExtra {
            FleetMenu(fleet: fleet)
                .onAppear { fleet.refresh() }
        } label: {
            Image(systemName: fleet.menuState.symbol)
                .accessibilityLabel(fleet.menuState.title)
        }
        .menuBarExtraStyle(.window)
    }
}

private struct FleetMenu: View {
    @ObservedObject var fleet: FleetModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label(fleet.menuState.title, systemImage: fleet.menuState.symbol)
                Spacer()
                Button("Refresh") { fleet.refresh() }.disabled(fleet.state == .scanning)
            }
            if fleet.boxes.isEmpty {
                Text(message).foregroundStyle(.secondary)
            } else {
                ForEach(fleet.boxes) { box in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack { Text(box.summary.node.host).fontWeight(.semibold); Spacer(); Text(box.summary.node.mode).foregroundStyle(.secondary) }
                        Text("CPU \(GravePresentation.percent(box.summary.resources.cpu_pct)) · Memory \(GravePresentation.percent(box.summary.resources.memory_pct)) · Disk \(GravePresentation.percent(box.summary.resources.disk_pct))")
                        if box.summary.resources.cpu_temp_c != nil || box.summary.resources.gpu_temp_c != nil {
                            Text("CPU temp \(GravePresentation.temperature(box.summary.resources.cpu_temp_c)) · GPU temp \(GravePresentation.temperature(box.summary.resources.gpu_temp_c))")
                                .foregroundStyle(.secondary)
                        }
                        Text("Sessions \(box.summary.activity.sessions_live) live / \(box.summary.activity.sessions_frozen) frozen · \(box.summary.health.services_failed) failed services · \(box.summary.health.containers_problem) container problems")
                            .foregroundStyle(box.summary.problems > 0 ? .orange : .secondary)
                        if fleet.dashboardURL(for: box) != nil { Button("Open Dashboard") { fleet.openDashboard(box) } }
                    }
                    Divider()
                }
            }
            Button("Quit") { NSApplication.shared.terminate(nil) }
        }
        .padding()
        .frame(width: 420)
    }

    private var message: String {
        switch fleet.state {
        case .missingTailscale: return "Tailscale is not installed."
        case .loggedOut: return "Sign in to Tailscale, then refresh."
        case .scanning: return "Discovering tailnet graves…"
        default: return "No reachable Gravedecay summaries."
        }
    }
}
