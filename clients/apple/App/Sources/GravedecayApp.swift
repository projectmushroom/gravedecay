import SwiftUI
import GravedecayKit

@main
struct GravedecayApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
        }
        #if os(macOS)
        Settings {
            SettingsView()
                .environmentObject(model)
                .frame(width: 420)
                .padding()
        }
        #endif
    }
}

struct ContentView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
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
