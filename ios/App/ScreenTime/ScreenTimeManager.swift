import DeviceActivity
import FamilyControls
import Foundation
import Observation

extension DeviceActivityName {
    /// 夜の時間帯（20時〜翌5時）
    static let night = Self("night")
}

/// スクリーンタイムの節目の記録（docs/phase1-design.md の 6.5）。
/// 夜の時間帯に、選んだアプリの使用時間の合計が一定の分（5分）増えるたびに、拡張機能が時刻を記録する。
/// 使用時間の数字そのものは Apple の決まりでアプリの外に出せないので、「節目の時刻」だけを使う。
@MainActor
@Observable
final class ScreenTimeManager {
    static let shared = ScreenTimeManager()

    var selection = FamilyActivitySelection()
    private(set) var isAuthorized = AuthorizationCenter.shared.authorizationStatus == .approved
    private(set) var stepMinutes: Int? = UserDefaults.standard.object(forKey: "screenTime.stepMinutes") as? Int
    private(set) var message: String?

    var hasSelection: Bool {
        !selection.applicationTokens.isEmpty || !selection.categoryTokens.isEmpty || !selection.webDomainTokens.isEmpty
    }

    init() {
        if let data = SharedStore.screenTimeSelection,
           let saved = try? JSONDecoder().decode(FamilyActivitySelection.self, from: data) {
            selection = saved
        }
    }

    func requestAuthorization() async {
        do {
            try await AuthorizationCenter.shared.requestAuthorization(for: .individual)
            isAuthorized = AuthorizationCenter.shared.authorizationStatus == .approved
            message = isAuthorized ? nil : "スクリーンタイムが許可されませんでした"
        } catch {
            message = "スクリーンタイムを許可できませんでした: \(error.localizedDescription)"
        }
    }

    /// 選んだアプリを保存して、毎晩の見張りを始める。
    /// 1晩に登録できる節目の数に上限があるかわからないので、5分刻みでだめなら10分・15分刻みにする
    func startMonitoring() {
        guard hasSelection else {
            message = "見張るアプリを選んでください（「すべてのアプリとカテゴリ」を選ぶのがおすすめです）"
            return
        }
        if let data = try? JSONEncoder().encode(selection) {
            SharedStore.screenTimeSelection = data
        }
        let center = DeviceActivityCenter()
        let schedule = DeviceActivitySchedule(
            intervalStart: DateComponents(hour: 20, minute: 0),
            intervalEnd: DateComponents(hour: 5, minute: 0),
            repeats: true
        )
        var lastError: Error?
        for step in [5, 10, 15] {
            var events: [DeviceActivityEvent.Name: DeviceActivityEvent] = [:]
            for minutes in stride(from: step, through: 360, by: step) {
                events[DeviceActivityEvent.Name("usage-\(minutes)")] = DeviceActivityEvent(
                    applications: selection.applicationTokens,
                    categories: selection.categoryTokens,
                    webDomains: selection.webDomainTokens,
                    threshold: DateComponents(hour: minutes / 60, minute: minutes % 60)
                )
            }
            do {
                center.stopMonitoring([.night])
                try center.startMonitoring(.night, during: schedule, events: events)
                stepMinutes = step
                UserDefaults.standard.set(step, forKey: "screenTime.stepMinutes")
                message = "毎晩20時〜翌5時、\(step)分ごとに記録します"
                return
            } catch {
                lastError = error
            }
        }
        message = "見張りを始められませんでした: \(lastError?.localizedDescription ?? "")"
    }

    func stopMonitoring() {
        DeviceActivityCenter().stopMonitoring([.night])
        stepMinutes = nil
        UserDefaults.standard.removeObject(forKey: "screenTime.stepMinutes")
        message = "見張りを止めました"
    }
}
