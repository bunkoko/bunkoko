import Foundation

/// 記録の種類（サーバーの kind と同じ）
enum LogKind: String, Codable {
    case meal, morningFeeling, dayCheckin, bath, caffeine, alcohol, charger, screenUsage
}

/// 端末の中の記録
struct LocalLog: Codable, Identifiable, Hashable {
    let id: String
    var kind: LogKind
    var occurredAt: Date
    var data: LogData
    var deleted = false
    /// サーバーに送り終えたか
    var uploaded = false
}

/// 手入力・ショートカット・スクリーンタイムの記録を保存し、サーバーに送る。
/// 通信できないときは端末に残しておき、次の機会に送り直す。
actor LogStore {
    static let shared = LogStore()

    private var logs: [LocalLog] = []
    private let fileURL: URL
    /// 端末に残しておく件数（送り終えた古いものから消す）
    private let keep = 1_000

    init() {
        let directory = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        fileURL = directory.appending(path: "logs.json")
        if let data = try? Data(contentsOf: fileURL), let saved = try? JSONDecoder().decode([LocalLog].self, from: data) {
            logs = saved
        }
    }

    /// 記録を足す（同じ id があれば置き換える）。送るのは flush のとき
    @discardableResult
    func add(_ kind: LogKind, data: LogData = LogData(), at date: Date = Date(), id: String = UUID().uuidString) -> LocalLog {
        let log = LocalLog(id: id, kind: kind, occurredAt: date, data: data)
        if let index = logs.firstIndex(where: { $0.id == id }) {
            logs[index] = log
        } else {
            logs.append(log)
        }
        save()
        return log
    }

    /// 1日のふり返りを、入れた項目だけ書き換える（nil の項目は今の値を残す）。書き換えたあとの値を返す。
    /// アプリの画面と通知のボタンの両方から入れるので、読んで書くまでを、この中で一度に行う
    @discardableResult
    func updateCheckin(day: String, id: String, condition: Int?, stress: Int?) -> LogData {
        let current = log(id: id)?.data
        let data = LogData(day: day, condition: condition ?? current?.condition, stress: stress ?? current?.stress)
        add(.dayCheckin, data: data, id: id)
        return data
    }

    func delete(id: String) {
        guard let index = logs.firstIndex(where: { $0.id == id }) else { return }
        logs[index].deleted = true
        logs[index].uploaded = false
        save()
    }

    /// id で探す（取り消したものは返さない）
    func log(id: String) -> LocalLog? {
        logs.first { $0.id == id && !$0.deleted }
    }

    func recent(_ kind: LogKind, limit: Int = 20) -> [LocalLog] {
        Array(logs.filter { $0.kind == kind && !$0.deleted }.sorted { $0.occurredAt > $1.occurredAt }.prefix(limit))
    }

    var pendingCount: Int { logs.filter { !$0.uploaded }.count }

    /// まだ送っていない記録を送る
    func flush() async throws {
        let pending = logs.filter { !$0.uploaded }
        guard !pending.isEmpty else { return }
        for start in stride(from: 0, to: pending.count, by: 500) {
            let part = Array(pending[start..<min(start + 500, pending.count)])
            var batch = LogsBatch()
            batch.logs = part.filter { !$0.deleted }.map {
                LogDTO(id: $0.id, kind: $0.kind.rawValue, occurredAt: $0.occurredAt.epochMs, data: $0.data)
            }
            batch.deleted = part.filter(\.deleted).map(\.id)
            try await APIClient.shared.uploadLogs(batch)
            // 送っている間に書き換わった記録（同じ id の新しい値）は、まだ送っていないものとして残す
            let sent = Set(part)
            for index in logs.indices where sent.contains(logs[index]) {
                logs[index].uploaded = true
            }
            save()
        }
    }

    private func save() {
        // 送り終えた古い記録から消して、件数を抑える
        if logs.count > keep {
            let removable = logs.enumerated()
                .filter { $0.element.uploaded }
                .sorted { $0.element.occurredAt < $1.element.occurredAt }
                .prefix(logs.count - keep)
                .map { $0.offset }
            for index in removable.sorted(by: >) { logs.remove(at: index) }
        }
        if let data = try? JSONEncoder().encode(logs) {
            try? data.write(to: fileURL, options: .atomic)
        }
    }
}
