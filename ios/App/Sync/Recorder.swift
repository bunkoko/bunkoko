import Foundation

/// 画面やショートカットから使う「記録する」操作。記録を保存し、ヘルスケアに書き込み、すぐに送ってみる
enum Recorder {
    static func meal(text: String, amount: String?, meal: String?, at date: Date) async {
        await LogStore.shared.add(.meal, data: LogData(text: text, amount: amount, meal: meal), at: date)
        await sendSoon()
    }

    static func morningFeeling(_ score: Int) async {
        await LogStore.shared.add(.morningFeeling, data: LogData(score: score))
        await sendSoon()
    }

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
    private static func sendSoon() async {
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
