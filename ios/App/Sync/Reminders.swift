import Foundation
import UserNotifications

/// 通知の種類（ボタンの組）。朝の体感と、夜のふり返りの1段目（調子）・2段目（ストレス）
enum ReminderStep: String, CaseIterable {
    case morning, condition, stress

    var categoryIdentifier: String { "reminder.\(rawValue)" }

    init?(categoryIdentifier: String) {
        guard let step = ReminderStep.allCases.first(where: { $0.categoryIdentifier == categoryIdentifier }) else { return nil }
        self = step
    }

    var title: String {
        switch self {
        case .morning: return "おはようございます"
        case .condition: return "今日のふり返り（1/2）"
        case .stress: return "今日のふり返り（2/2）"
        }
    }

    var body: String {
        switch self {
        case .morning: return "起きたときの感じを 1（ぐったり）〜5（すっきり）で選んでください。iPhone では通知を長押しすると選べます"
        case .condition: return "今日の調子を 1（悪い）〜5（よい）で選んでください"
        case .stress: return "今日のストレスを 1（なかった）〜5（とても強かった）で選んでください"
        }
    }

    /// 1〜5 のボタンの文字
    var scoreTitles: [String] {
        switch self {
        case .morning: return ["1（ぐったり）", "2", "3", "4", "5（すっきり）"]
        case .condition: return ["1（悪い）", "2", "3", "4", "5（よい）"]
        case .stress: return ["1（なかった）", "2", "3", "4", "5（とても強かった）"]
        }
    }
}

/// 朝の体感と1日のふり返りの入れ忘れを知らせる、端末の中の通知（docs/analysis.md の 2.7）。
/// サーバーを使わないので、無料の Apple ID で入れたアプリでも使える。
/// 先の7日分を予約し、入れた日の分は予約しない（入れるたび・アプリを開くたびに予約し直す）。
/// 通知のボタンから、その場で答えられる（iPhone がロック中なら Apple Watch でも）
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
        // 予約し直すのは、ここで予約する朝と夜のお知らせだけ（通知から続けて出したストレスの質問は消さない）
        let scheduled = pending.map(\.identifier).filter { $0.hasPrefix("\(prefix)morning.") || $0.hasPrefix("\(prefix)evening.") }
        center.removePendingNotificationRequests(withIdentifiers: scheduled)
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
                    await schedule(id: "\(prefix)morning.\(day)", at: fire, content: content(for: .morning, day: day))
                }
            }

            if let bedtime = bedtime(for: date) {
                let fire = bedtime.addingTimeInterval(-60 * 60)
                if fire > now, await LogStore.shared.log(id: Recorder.checkinID(day: day)) == nil {
                    await schedule(id: "\(prefix)evening.\(day)", at: fire, content: content(for: .condition, day: day))
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

    // MARK: ボタン

    /// いちばん上のボタン（何も記録しない）。Apple Watch のダブルタップは、いちばん上のボタンを押すので、点数を先頭に置かない
    static let laterAction = "later"

    static func scoreAction(_ score: Int) -> String { "score.\(score)" }

    /// ボタンの id から点数（1〜5）。点数のボタンでなければ nil
    static func score(fromAction id: String) -> Int? {
        guard id.hasPrefix("score."), let score = Int(id.dropFirst("score.".count)), (1...5).contains(score) else { return nil }
        return score
    }

    /// 通知の種類（ボタンの組）。アプリを起動するたびに登録する
    static var categories: Set<UNNotificationCategory> {
        Set(ReminderStep.allCases.map { step in
            let later = UNNotificationAction(identifier: laterAction, title: "あとで入れる", options: [])
            let scores = step.scoreTitles.enumerated().map { index, title in
                UNNotificationAction(identifier: scoreAction(index + 1), title: title, options: [])
            }
            return UNNotificationCategory(identifier: step.categoryIdentifier, actions: [later] + scores, intentIdentifiers: [], options: [])
        })
    }

    /// 通知の中身。どの日の分かを入れておく（夜中の0時を過ぎてから答えても、その日の分にする）
    static func content(for step: ReminderStep, day: String) -> UNMutableNotificationContent {
        let content = UNMutableNotificationContent()
        content.title = step.title
        content.body = step.body
        content.sound = .default
        content.categoryIdentifier = step.categoryIdentifier
        content.userInfo = ["day": day]
        return content
    }

    // MARK: ボタンの答え

    /// 通知のボタンの答えを記録する。アプリが閉じていても、iPhone のアプリが裏で受け取る（Apple Watch で押した分も）
    static func handle(_ response: UNNotificationResponse) async {
        // ［あとで入れる］・通知そのものを押した・閉じたときは、何もしない
        guard let value = score(fromAction: response.actionIdentifier) else { return }
        let notice = response.notification.request.content
        guard let step = ReminderStep(categoryIdentifier: notice.categoryIdentifier), let day = notice.userInfo["day"] as? String else { return }
        switch step {
        case .morning:
            // 体感はその朝のものだけ（前の日の通知を、次の日に押しても記録しない）
            guard day == Fmt.dateString(Date()) else { return }
            await Recorder.morningFeeling(value)
        case .condition:
            // その日のふり返りの調子だけを書き換える（ストレスがもう入っていれば残る）。
            // 端末に保存したら、送るのを待たずにストレスを聞き（もう入っていれば聞かない）、最後に送る
            let data = await Recorder.saveDayCheckin(day: day, condition: value, stress: nil)
            if data?.stress == nil {
                let request = UNNotificationRequest(identifier: "reminder.stress.\(day)", content: content(for: .stress, day: day), trigger: nil)
                try? await UNUserNotificationCenter.current().add(request)
            }
            await Recorder.sendSoon()
        case .stress:
            // ストレスだけを書き換える（調子は残る）
            await Recorder.dayCheckin(day: day, condition: nil, stress: value)
        }
    }

    private static func schedule(id: String, at date: Date, content: UNMutableNotificationContent) async {
        let components = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute], from: date)
        let trigger = UNCalendarNotificationTrigger(dateMatching: components, repeats: false)
        try? await UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: id, content: content, trigger: trigger))
    }
}
