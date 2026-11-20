import Foundation

/// サーバーの設定のうち、端末の通知（リマインダー）に使うものの控え。
/// サーバーにつながらないときも、リマインダーを決まった時刻に出せるように端末にも残す
enum LocalSettings {
    private static let defaults = UserDefaults.standard

    static var targetBedtime: String {
        get { defaults.string(forKey: "local.targetBedtime") ?? "23:30" }
        set { defaults.set(newValue, forKey: "local.targetBedtime") }
    }

    static var targetWake: String {
        get { defaults.string(forKey: "local.targetWake") ?? "07:00" }
        set { defaults.set(newValue, forKey: "local.targetWake") }
    }

    /// サーバーから朝のレポートが届くか（届くなら、朝のリマインダーは出さない）
    static var serverMorningReport: Bool {
        get { defaults.bool(forKey: "local.serverMorningReport") }
        set { defaults.set(newValue, forKey: "local.serverMorningReport") }
    }

    static func remember(_ settings: ServerSettings) {
        targetBedtime = settings.targetBedtime
        targetWake = settings.targetWake
        serverMorningReport = settings.notifications.morningReport
    }
}

/// 記録をどの日の分にするか
enum DayKey {
    /// 夜中の0時〜4時は、まだ前の日の続き（寝る前）とみなす
    static let rolloverHour = 4

    /// 今「起きている日」。1日のふり返りは、この日の分として記録する
    static func activeDay(_ now: Date = Date()) -> String {
        Fmt.dateString(Calendar.current.date(byAdding: .hour, value: -rolloverHour, to: now) ?? now)
    }

    /// "2026-11-20" の n 日後（マイナスなら前）
    static func adding(_ days: Int, to day: String) -> String {
        guard let date = Fmt.parseDate(day), let moved = Calendar.current.date(byAdding: .day, value: days, to: date) else { return day }
        return Fmt.dateString(moved)
    }

    /// 画面に出す呼び方（今日・昨日・11/18（水））
    static func label(_ day: String, now: Date = Date()) -> String {
        let active = activeDay(now)
        if day == active { return "今日" }
        if day == adding(-1, to: active) { return "昨日" }
        return Fmt.dateLabel(day)
    }
}
