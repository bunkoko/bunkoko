import Foundation

/// 画面やショートカットから使う「記録する」操作。記録を保存し、ヘルスケアに書き込み、すぐに送ってみる
enum Recorder {
    static func meal(text: String, amount: String?, meal: String?, at date: Date) async {
        await LogStore.shared.add(.meal, data: LogData(text: text, amount: amount, meal: meal), at: date)
        await sendSoon()
    }

    /// 朝の体感。1日に1つ（入れ直したら上書き）
    static func morningFeeling(_ score: Int, at date: Date = Date()) async {
        await LogStore.shared.add(.morningFeeling, data: LogData(score: score), at: date, id: feelingID(day: Fmt.dateString(date)))
        await Reminders.refresh()
        await sendSoon()
    }

    /// 1日のふり返り。ふり返る日ごとに1つ。入れた項目だけ書き換え、nil の項目は今の値を残す。書き換えたあとの値を返す
    @discardableResult
    static func dayCheckin(day: String, condition: Int?, stress: Int?) async -> LogData? {
        let data = await saveDayCheckin(day: day, condition: condition, stress: stress)
        await sendSoon()
        return data
    }

    /// 1日のふり返りを端末に保存し、リマインダーを予約し直す（送るのは sendSoon。通知から続けて聞くとき、送るのを待たないため）
    static func saveDayCheckin(day: String, condition: Int?, stress: Int?) async -> LogData? {
        guard condition != nil || stress != nil else { return nil }
        let data = await LogStore.shared.updateCheckin(day: day, id: checkinID(day: day), condition: condition, stress: stress)
        await Reminders.refresh()
        return data
    }

    static func feelingID(day: String) -> String { "feeling-\(day)" }
    static func checkinID(day: String) -> String { "checkin-\(day)" }

    static func bath(at date: Date = Date()) async {
        await LogStore.shared.add(.bath, at: date)
        await sendSoon()
    }

    static func caffeine(item: String, mg: Double, at date: Date = Date()) async {
        let log = await LogStore.shared.add(.caffeine, data: LogData(item: item, mg: mg), at: date)
        try? await HealthSync.shared.saveCaffeine(mg: mg, at: date, logID: log.id)
        await sendSoon()
    }

    static func alcohol(item: String, drinks: Double, at date: Date = Date()) async {
        let log = await LogStore.shared.add(.alcohol, data: LogData(item: item, drinks: drinks), at: date)
        try? await HealthSync.shared.saveAlcohol(drinks: drinks, at: date, logID: log.id)
        await sendSoon()
    }

    static func charger(connected: Bool, at date: Date = Date()) async {
        await LogStore.shared.add(.charger, data: LogData(state: connected ? "connected" : "disconnected"), at: date)
        await sendSoon()
    }

    static func delete(id: String) async {
        await LogStore.shared.delete(id: id)
        await sendSoon()
    }

    /// 送れなければ端末に残り、次の同期で送られる
    static func sendSoon() async {
        try? await LogStore.shared.flush()
    }
}

/// スクリーンタイムの節目（拡張機能が App Group に書いたもの）を、記録としてサーバーに送る
enum ScreenTimeImporter {
    static func importEvents() async {
        let events = SharedStore.loadScreenEvents()
        guard !events.isEmpty else { return }
        for event in events {
            // 同じ節目を2回送らないように、時刻から id を作る
            await LogStore.shared.add(
                .screenUsage,
                data: LogData(cumulativeMinutes: event.cumulativeMinutes),
                at: event.at,
                id: "screen-\(event.at.epochMs)"
            )
        }
        SharedStore.removeScreenEvents(Set(events))
    }
}
