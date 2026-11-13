import FamilyControls
import SwiftUI

struct SettingsView: View {
    var onDisconnect: () -> Void

    @State private var settings: ServerSettings?
    @State private var bedtime = Date()
    @State private var wake = Date()
    @State private var status: HealthSync.Status?
    @State private var syncing = false
    @State private var message: String?
    @State private var screenTime = ScreenTimeManager.shared
    @State private var showPicker = false
    @State private var urlText = AppConfig.serverURL?.absoluteString ?? ""
    @State private var tokenText = ""
    @AppStorage("health.authorizationRequested") private var healthRequested = false

    var body: some View {
        NavigationStack {
            Form {
                if let message {
                    Section { Text(message).foregroundStyle(.secondary) }
                }

                Section("目標の時刻") {
                    DatePicker("就寝", selection: $bedtime, displayedComponents: .hourAndMinute)
                    DatePicker("起床", selection: $wake, displayedComponents: .hourAndMinute)
                }

                Section("通知") {
                    if !AppConfig.hasPaidFeatures {
                        PaidFeatureNote()
                    } else if settings != nil {
                        Toggle("寝る前の確認", isOn: notificationBinding(\.bedtime))
                        Stepper("就寝の \(settings?.notifications.bedtimeLeadMinutes ?? 60) 分前に確認", value: leadBinding, in: 15...180, step: 15)
                        Toggle("朝のレポート", isOn: notificationBinding(\.morningReport))
                    }
                    if AppConfig.hasPaidFeatures {
                        Button("通知を許可する") {
                            Task {
                                let granted = await NotificationPermission.request()
                                message = granted ? "通知を許可しました" : "通知が許可されませんでした（iPhone の設定アプリから変えられます）"
                            }
                        }
                    }
                }

                Section {
                    Button("目標の時刻と通知の設定をサーバーに保存") {
                        Task { await saveSettings() }
                    }
                    .disabled(settings == nil)
                }

                Section("ヘルスケア") {
                    Button("ヘルスケアへのアクセスを許可") {
                        Task { await requestHealth() }
                    }
                    Button(syncing ? "送っています…" : "今すぐ送る") {
                        Task { await syncNow() }
                    }
                    .disabled(syncing)
                    if let status {
                        MetricRow(label: "最後に送った時刻", value: status.lastSync.map { $0.formatted(date: .abbreviated, time: .shortened) } ?? "まだ")
                        MetricRow(label: "過去分の取り込み", value: backfillProgress(status))
                        if let error = status.lastError {
                            Text(error).font(.caption).foregroundStyle(.red)
                        }
                    }
                    Button("過去1年分を送り直す", role: .destructive) {
                        Task {
                            await HealthSync.shared.resetAndResendEverything()
                            await syncNow()
                        }
                    }
                }

                Section("スクリーンタイム（夜にスマホを最後に使った時刻）") {
                    if !AppConfig.hasPaidFeatures {
                        PaidFeatureNote()
                    } else if !screenTime.isAuthorized {
                        Button("スクリーンタイムを許可") {
                            Task { await screenTime.requestAuthorization() }
                        }
                    } else {
                        Button("見張るアプリを選ぶ（「すべて」がおすすめ）") { showPicker = true }
                        if let step = screenTime.stepMinutes {
                            MetricRow(label: "記録", value: "毎晩20時〜翌5時・\(step)分ごと")
                            Button("見張りを止める", role: .destructive) { screenTime.stopMonitoring() }
                        }
                    }
                    if let note = screenTime.message {
                        Text(note).font(.caption).foregroundStyle(.secondary)
                    }
                }

                Section("充電の記録（ショートカット）") {
                    Text("""
                    ショートカットアプリ →「オートメーション」→「＋」→「充電器」で、
                    ① 「接続されたとき」→ アクション「充電を記録」（状態: つないだ）
                    ② 「接続が解除されたとき」→ アクション「充電を記録」（状態: 外した）
                    を作り、「すぐに実行」にしてください。
                    """)
                    .font(.callout)
                }

                ServerFields(urlText: $urlText, tokenText: $tokenText)
                Section {
                    Button("サーバーの設定を保存して確かめる") {
                        Task {
                            let error = await ServerConnection.save(urlText: urlText, tokenText: tokenText)
                            message = error ?? "サーバーにつながりました"
                            if error == nil { await loadSettings() }
                        }
                    }
                    Button("この iPhone の接続を解除", role: .destructive) {
                        AppConfig.appToken = nil
                        onDisconnect()
                    }
                }

                Section("このアプリについて") {
                    MetricRow(label: "バージョン", value: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "")
                }
            }
            .navigationTitle("設定")
            .task {
                await loadSettings()
                status = await HealthSync.shared.status()
            }
            .familyActivityPicker(isPresented: $showPicker, selection: $screenTime.selection)
            .onChange(of: showPicker) { _, shown in
                if !shown { screenTime.startMonitoring() }
            }
        }
    }

    // MARK: - 設定の読み書き

    private func notificationBinding(_ keyPath: WritableKeyPath<NotificationSettings, Bool>) -> Binding<Bool> {
        Binding(
            get: { settings?.notifications[keyPath: keyPath] ?? true },
            set: { settings?.notifications[keyPath: keyPath] = $0 }
        )
    }

    private var leadBinding: Binding<Int> {
        Binding(
            get: { settings?.notifications.bedtimeLeadMinutes ?? 60 },
            set: { settings?.notifications.bedtimeLeadMinutes = $0 }
        )
    }

    private func loadSettings() async {
        do {
            let loaded = try await APIClient.shared.settings()
            settings = loaded
            bedtime = TimeOfDay.date(from: loaded.targetBedtime)
            wake = TimeOfDay.date(from: loaded.targetWake)
        } catch {
            message = error.localizedDescription
        }
    }

    private func saveSettings() async {
        guard var next = settings else { return }
        next.targetBedtime = TimeOfDay.string(from: bedtime)
        next.targetWake = TimeOfDay.string(from: wake)
        next.timeZone = TimeZone.current.identifier
        do {
            settings = try await APIClient.shared.saveSettings(next)
            message = "保存しました"
        } catch {
            message = error.localizedDescription
        }
    }

    // MARK: - ヘルスケア

    private func requestHealth() async {
        do {
            try await HealthSync.shared.requestAuthorization()
            healthRequested = true
            message = "ヘルスケアの許可を確認しました。過去1年分を送ります（数分〜数十分かかります）"
            await HealthSync.shared.startBackgroundDelivery()
            await syncNow()
        } catch {
            message = "ヘルスケアを許可できませんでした: \(error.localizedDescription)"
        }
    }

    private func syncNow() async {
        syncing = true
        await HealthSync.shared.sync()
        status = await HealthSync.shared.status()
        syncing = false
        message = status?.lastError == nil ? "送りました" : "送れなかった分は、次の機会に送り直します"
    }

    private func backfillProgress(_ status: HealthSync.Status) -> String {
        guard let until = status.bucketsSentUntil else { return "まだ" }
        let total = Date().timeIntervalSince(status.backfillStart)
        let done = until.timeIntervalSince(status.backfillStart)
        guard total > 0 else { return "完了" }
        let ratio = min(1, max(0, done / total))
        return ratio >= 0.999 ? "完了" : "\(Int(ratio * 100))%"
    }
}

/// 無料の Apple ID でビルドしたときに、使えない機能の代わりに出す説明
struct PaidFeatureNote: View {
    var body: some View {
        Text("Apple Developer Program（有料）に登録すると使えます。登録したら、ios/Config/Local.xcconfig の KENKO_EDITION を Paid にして、ビルドし直してください。")
            .font(.callout)
            .foregroundStyle(.secondary)
    }
}

/// "23:30" と Date の変換（目標の時刻）
enum TimeOfDay {
    static func date(from hhmm: String) -> Date {
        let parts = hhmm.split(separator: ":").compactMap { Int($0) }
        var components = Calendar.current.dateComponents([.year, .month, .day], from: Date())
        components.hour = parts.first ?? 23
        components.minute = parts.count > 1 ? parts[1] : 0
        return Calendar.current.date(from: components) ?? Date()
    }

    static func string(from date: Date) -> String {
        let components = Calendar.current.dateComponents([.hour, .minute], from: date)
        return String(format: "%02d:%02d", components.hour ?? 0, components.minute ?? 0)
    }
}
