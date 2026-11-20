import Foundation
import UserNotifications

/// 朝の体感と1日のふり返りの入れ忘れを知らせる、端末の中の通知（docs/analysis.md の 2.7）。
/// サーバーを使わないので、無料の Apple ID で入れたアプリでも使える。
/// 先の7日分を予約し、入れた日の分は予約しない（入れるたび・アプリを開くたびに予約し直す）
enum Reminders {
    static let enabledKey = "reminders.enabled"
    private static let prefix = "reminder."
    private static let daysAhead = 7

    static var isEnabled: Bool {
        UserDefaults.standard.object(forKey: enabledKey) as? Bool ?? true
    }

    static func refresh() async {
        let center = UNUserNotificationCenter.current()
        let pending = await center.pendingNotificationRequests()
        center.removePendingNotificationRequests(withIdentifiers: pending.map(\.identifier).filter { $0.hasPrefix(prefix) })
        guard isEnabled else { return }
        let status = await center.notificationSettings().authorizationStatus
        guard status == .authorized || status == .provisional || status == .ephemeral else { return }

        let now = Date()
        // サーバーから朝のレポートが届くなら、朝のリマインダーは重ねて出さない
        let morning = !(AppConfig.hasPaidFeatures && LocalSettings.serverMorningReport)
        for offset in 0..<daysAhead {
            guard let date = Calendar.current.date(byAdding: .day, value: offset, to: now) else { continue }
            let day = Fmt.dateString(date)

            if morning, let wake = TimeOfDay.date(on: date, hhmm: LocalSettings.targetWake) {
                let fire = wake.addingTimeInterval(30 * 60)
                if fire > now, await LogStore.shared.log(id: Recorder.feelingID(day: day)) == nil {
                    await schedule(id: "\(prefix)morning.\(day)", at: fire, title: "おはようございます", body: "今朝のすっきり度を記録しましょう（1タップ）")
                }
            }

            if let bedtime = bedtime(for: date) {
                let fire = bedtime.addingTimeInterval(-60 * 60)
                if fire > now, await LogStore.shared.log(id: Recorder.checkinID(day: day)) == nil {
                    await schedule(id: "\(prefix)evening.\(day)", at: fire, title: "今日のふり返り", body: "今日の調子・眠気・ストレスを記録しましょう（3タップ）")
                }
            }
        }
    }

    /// その日の夜の目標の就寝時刻。12時より前の時刻（例: 0:30）なら、日付が変わったあと
    private static func bedtime(for date: Date) -> Date? {
        let hhmm = LocalSettings.targetBedtime
        let hour = Int(hhmm.prefix(2)) ?? 23
        let night = hour < 12 ? Calendar.current.date(byAdding: .day, value: 1, to: date) : date
        return night.flatMap { TimeOfDay.date(on: $0, hhmm: hhmm) }
    }

    private static func schedule(id: String, at date: Date, title: String, body: String) async {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        let components = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute], from: date)
        let trigger = UNCalendarNotificationTrigger(dateMatching: components, repeats: false)
        try? await UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: id, content: content, trigger: trigger))
    }
}
