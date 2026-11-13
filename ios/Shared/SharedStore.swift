import Foundation

/// アプリ本体と拡張機能（スクリーンタイムの節目の記録）の間で、データを受け渡す場所（App Group）。
/// 拡張機能は通信できないので、ここに書いておき、アプリが次に動いたときにサーバーへ送る。
enum SharedStore {
    static var appGroupID: String {
        Bundle.main.object(forInfoDictionaryKey: "AppGroupIdentifier") as? String ?? ""
    }

    private static var defaults: UserDefaults? {
        UserDefaults(suiteName: appGroupID)
    }

    private static let screenEventsKey = "screenUsageEvents"
    private static let selectionKey = "screenTimeSelection"
    private static let maxEvents = 1_000

    /// スクリーンタイムの節目: その時刻に、夜の時間帯の使用時間の合計が cumulativeMinutes 分になった
    struct ScreenEvent: Codable, Hashable {
        let at: Date
        let cumulativeMinutes: Int
    }

    static func appendScreenEvent(_ event: ScreenEvent) {
        guard let defaults else { return }
        var events = loadScreenEvents()
        events.append(event)
        if events.count > maxEvents { events.removeFirst(events.count - maxEvents) }
        if let data = try? JSONEncoder().encode(events) {
            defaults.set(data, forKey: screenEventsKey)
        }
    }

    static func loadScreenEvents() -> [ScreenEvent] {
        guard let data = defaults?.data(forKey: screenEventsKey),
              let events = try? JSONDecoder().decode([ScreenEvent].self, from: data) else { return [] }
        return events
    }

    /// サーバーに送り終えた節目を消す
    static func removeScreenEvents(_ sent: Set<ScreenEvent>) {
        guard let defaults else { return }
        let rest = loadScreenEvents().filter { !sent.contains($0) }
        if let data = try? JSONEncoder().encode(rest) {
            defaults.set(data, forKey: screenEventsKey)
        }
    }

    /// 見張る対象として選んだアプリ（FamilyActivitySelection を JSON にしたもの）
    static var screenTimeSelection: Data? {
        get { defaults?.data(forKey: selectionKey) }
        set { defaults?.set(newValue, forKey: selectionKey) }
    }
}
