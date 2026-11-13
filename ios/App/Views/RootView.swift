import SwiftUI

struct RootView: View {
    @State private var configured = AppConfig.isConfigured

    var body: some View {
        if configured {
            TabView {
                Tab("ホーム", systemImage: "house") { HomeView() }
                Tab("夜", systemImage: "moon.zzz") { NightListView() }
                Tab("推移", systemImage: "chart.xyaxis.line") { TrendsView() }
                Tab("記録", systemImage: "square.and.pencil") { RecordView() }
                Tab("設定", systemImage: "gearshape") { SettingsView(onDisconnect: { configured = false }) }
            }
        } else {
            ServerSetupView { configured = true }
        }
    }
}

/// はじめに、サーバーの URL と合言葉（APP_TOKEN）を入れる画面
struct ServerSetupView: View {
    var onDone: () -> Void
    @State private var urlText = AppConfig.serverURL?.absoluteString ?? ""
    @State private var tokenText = ""
    @State private var message: String?
    @State private var checking = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("サーバー（server/README.md の手順で公開したもの）の URL と、APP_TOKEN を入れてください。")
                        .font(.callout)
                }
                ServerFields(urlText: $urlText, tokenText: $tokenText)
                Section {
                    Button(checking ? "確かめています…" : "保存して確かめる") {
                        Task { await connect() }
                    }
                    .disabled(checking || urlText.isEmpty || tokenText.isEmpty)
                }
                if let message {
                    Section { Text(message).foregroundStyle(.red) }
                }
            }
            .navigationTitle("はじめに")
        }
    }

    private func connect() async {
        checking = true
        defer { checking = false }
        if let error = await ServerConnection.save(urlText: urlText, tokenText: tokenText) {
            message = error
        } else {
            onDone()
        }
    }
}

struct ServerFields: View {
    @Binding var urlText: String
    @Binding var tokenText: String

    var body: some View {
        Section("サーバー") {
            TextField("https://bunkoko-server.xxxx.workers.dev", text: $urlText)
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            SecureField("APP_TOKEN", text: $tokenText)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
        }
    }
}

enum ServerConnection {
    /// URL と合言葉を保存して、つながるか確かめる。問題があれば理由を返す
    static func save(urlText: String, tokenText: String) async -> String? {
        let trimmed = urlText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed), url.scheme == "https", url.host != nil else {
            return "URL は https:// から始めてください"
        }
        let previousURL = AppConfig.serverURL
        let previousToken = AppConfig.appToken
        AppConfig.serverURL = url
        if !tokenText.isEmpty { AppConfig.appToken = tokenText.trimmingCharacters(in: .whitespacesAndNewlines) }
        do {
            _ = try await APIClient.shared.settings()
            return nil
        } catch {
            // つながらなければ、前の設定に戻す
            AppConfig.serverURL = previousURL
            AppConfig.appToken = previousToken
            return error.localizedDescription
        }
    }
}
