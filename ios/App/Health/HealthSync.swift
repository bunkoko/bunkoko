import Foundation
import HealthKit

/// HealthKit のデータをサーバーに送る（docs/phase1-design.md の 6.3）。
/// - 測った値: 型ごとに「前回どこまで送ったか」（アンカー）を覚えておき、増えた分と削除された分だけを送る
/// - 集計: 心拍数は5分ごと、歩数などは1時間ごとに集計して送る（最後の3時間は毎回送り直す。あとから届くデータがあるため）
/// - 初回は過去1年分を、少しずつ送る（途中で止まっても、次に起きたときに続きから送る）
actor HealthSync {
    static let shared = HealthSync()

    let store = HKHealthStore()
    private let defaults = UserDefaults.standard

    private var isSyncing = false
    private var observersStarted = false
    private(set) var lastError: String?

    /// 1回に問い合わせる・送る件数（サーバーは1回 1,000件まで）
    private let pageSize = 500
    private let uploadChunk = 900
    /// 集計を1回に計算する日数
    private let bucketChunkDays = 3.0

    static var isAvailable: Bool { HKHealthStore.isHealthDataAvailable() }

    // MARK: - 状態

    struct Status: Sendable {
        let lastSync: Date?
        let lastError: String?
        let isSyncing: Bool
        /// 集計をどこまで送ったか（過去分の取り込みの進み具合）
        let bucketsSentUntil: Date?
        let backfillStart: Date
    }

    func status() -> Status {
        Status(
            lastSync: defaults.object(forKey: Keys.lastSync) as? Date,
            lastError: lastError,
            isSyncing: isSyncing,
            bucketsSentUntil: defaults.object(forKey: Keys.bucketCursor) as? Date,
            backfillStart: backfillStart
        )
    }

    private enum Keys {
        static let lastSync = "health.lastSync"
        static let bucketCursor = "health.bucketCursor"
        static let backfillStart = "health.backfillStart"
        static func anchor(_ name: String) -> String { "health.anchor.\(name)" }
    }

    /// 過去分を取り込む始まり（初めて送るときに、その1年前の0時に決める）
    private var backfillStart: Date {
        if let saved = defaults.object(forKey: Keys.backfillStart) as? Date { return saved }
        let start = Calendar.current.startOfDay(for: Calendar.current.date(byAdding: .year, value: -1, to: Date()) ?? Date())
        defaults.set(start, forKey: Keys.backfillStart)
        return start
    }

    // MARK: - 許可とバックグラウンドでの受け取り

    func requestAuthorization() async throws {
        try await store.requestAuthorization(toShare: HealthTypes.writeTypes, read: HealthTypes.readTypes)
    }

    /// アプリが起動するたびに呼ぶ。新しいデータが記録されたら、iPhone がアプリを起こしてくれる
    func startBackgroundDelivery() async {
        guard Self.isAvailable, !observersStarted else { return }
        observersStarted = true
        for (type, frequency) in HealthTypes.observed {
            let query = HKObserverQuery(sampleType: type, predicate: nil) { _, completionHandler, error in
                guard error == nil else {
                    completionHandler()
                    return
                }
                Task {
                    await HealthSync.shared.sync()
                    completionHandler()
                }
            }
            store.execute(query)
            do {
                try await store.enableBackgroundDelivery(for: type, frequency: frequency)
            } catch {
                lastError = "バックグラウンドでの受け取りを設定できませんでした: \(error.localizedDescription)"
            }
        }
    }

    // MARK: - 送る

    /// 新しく増えた分を送る（何度呼んでもよい。同時には1つだけ動く）
    func sync() async {
        guard Self.isAvailable, AppConfig.isConfigured, !isSyncing else { return }
        isSyncing = true
        defer { isSyncing = false }
        do {
            try await syncSamples()
            try await syncBuckets(until: Date())
            await ScreenTimeImporter.importEvents()
            try await LogStore.shared.flush()
            defaults.set(Date(), forKey: Keys.lastSync)
            lastError = nil
        } catch {
            // 画面がロックされている間は HealthKit を読めないので、次の機会に続きから送る
            lastError = error.localizedDescription
        }
    }

    /// 送った位置を忘れて、過去1年分を送り直す
    func resetAndResendEverything() {
        for spec in HealthTypes.anchored { defaults.removeObject(forKey: Keys.anchor(spec.name)) }
        defaults.removeObject(forKey: Keys.bucketCursor)
        defaults.removeObject(forKey: Keys.backfillStart)
    }

    // MARK: 測った値（差分）

    private func syncSamples() async throws {
        let predicate = HKQuery.predicateForSamples(withStart: backfillStart, end: nil)
        for spec in HealthTypes.anchored {
            var anchor = loadAnchor(spec.name)
            while true {
                let descriptor = HKAnchoredObjectQueryDescriptor(
                    predicates: [.sample(type: spec.type, predicate: predicate)],
                    anchor: anchor,
                    limit: pageSize
                )
                let result = try await descriptor.result(for: store)

                var batch = HealthBatch()
                for sample in result.addedSamples {
                    encode(sample, spec: spec, into: &batch)
                }
                batch.deleted = result.deletedObjects.map { $0.uuid.uuidString }
                try await upload(batch)

                saveAnchor(result.newAnchor, name: spec.name)
                anchor = result.newAnchor
                if result.addedSamples.count + result.deletedObjects.count < pageSize { break }
            }
        }
    }

    private func encode(_ sample: HKSample, spec: SampleSpec, into batch: inout HealthBatch) {
        let source = sample.sourceRevision.source
        if spec.skipOwnSamples, source.bundleIdentifier == Bundle.main.bundleIdentifier { return }
        let sourceDTO = SourceDTO(name: source.name, productType: sample.sourceRevision.productType)
        let uuid = sample.uuid.uuidString
        let start = sample.startDate.epochMs
        let end = sample.endDate.epochMs

        if let workout = sample as? HKWorkout {
            batch.workouts.append(WorkoutDTO(
                uuid: uuid,
                activityType: HealthTypes.workoutName(workout.workoutActivityType),
                start: start,
                end: end,
                energyKcal: workout.statistics(for: HKQuantityType(.activeEnergyBurned))?.sumQuantity()?.doubleValue(for: .kilocalorie()),
                distanceM: workout.statistics(for: HKQuantityType(.distanceWalkingRunning))?.sumQuantity()?.doubleValue(for: .meter()),
                avgHeartRate: workout.statistics(for: HKQuantityType(.heartRate))?.averageQuantity()?.doubleValue(for: HealthTypes.bpm),
                source: sourceDTO
            ))
            return
        }
        if let mood = sample as? HKStateOfMind {
            batch.samples.append(SampleDTO(
                uuid: uuid, type: spec.name, start: start, end: end,
                value: mood.valence, valueText: mood.kind == .dailyMood ? "dailyMood" : "momentaryEmotion",
                unit: nil, source: sourceDTO
            ))
            return
        }
        if let quantity = sample as? HKQuantitySample, let unit = spec.unit {
            batch.samples.append(SampleDTO(
                uuid: uuid, type: spec.name, start: start, end: end,
                value: quantity.quantity.doubleValue(for: unit) * spec.scale, valueText: nil,
                unit: unit.unitString, source: sourceDTO
            ))
            return
        }
        if let category = sample as? HKCategorySample {
            if spec.name == "sleepAnalysis" {
                guard let stage = HealthTypes.sleepStageName(category.value) else { return }
                batch.samples.append(SampleDTO(uuid: uuid, type: spec.name, start: start, end: end, value: nil, valueText: stage, unit: nil, source: sourceDTO))
            } else {
                batch.samples.append(SampleDTO(uuid: uuid, type: spec.name, start: start, end: end, value: 1, valueText: nil, unit: nil, source: sourceDTO))
            }
        }
    }

    typealias SampleSpec = HealthTypes.SampleSpec

    // MARK: 集計

    private func syncBuckets(until end: Date) async throws {
        let calendar = Calendar.current
        let cursor = (defaults.object(forKey: Keys.bucketCursor) as? Date)?.addingTimeInterval(-3 * 3600) ?? backfillStart
        // 集計の区切りに合わせて、1時間の始まりから計算する（途中から計算すると、その時間の値が少なくなるため）
        var from = calendar.dateInterval(of: .hour, for: max(cursor, backfillStart))?.start ?? backfillStart

        while from < end {
            let chunkEnd = min(from.addingTimeInterval(bucketChunkDays * 86_400), end)
            var buckets = try await statistics(HealthTypes.heartRate, from: from, to: chunkEnd)
            for spec in HealthTypes.hourly {
                buckets += try await statistics(spec, from: from, to: chunkEnd)
            }
            try await upload(HealthBatch(buckets: buckets))
            defaults.set(chunkEnd, forKey: Keys.bucketCursor)
            from = chunkEnd
        }
    }

    private func statistics(_ spec: HealthTypes.BucketSpec, from: Date, to: Date) async throws -> [BucketDTO] {
        // 区切りをまたぐ記録も正しく分けられるように、少し前から問い合わせる
        let predicate = HKQuery.predicateForSamples(withStart: from.addingTimeInterval(-3600), end: to)
        let descriptor = HKStatisticsCollectionQueryDescriptor(
            predicate: .quantitySample(type: HKQuantityType(spec.identifier), predicate: predicate),
            options: spec.options,
            anchorDate: Calendar.current.startOfDay(for: from),
            intervalComponents: DateComponents(minute: spec.minutes)
        )
        let collection = try await descriptor.result(for: store)
        return collection.statistics().compactMap { stats -> BucketDTO? in
            guard stats.startDate >= from, stats.startDate < to else { return nil }
            let unit = spec.unit
            let sum = stats.sumQuantity()?.doubleValue(for: unit)
            let avg = stats.averageQuantity()?.doubleValue(for: unit)
            guard sum != nil || avg != nil else { return nil }
            return BucketDTO(
                type: spec.name,
                start: stats.startDate.epochMs,
                minutes: spec.minutes,
                sum: sum,
                avg: avg,
                min: stats.minimumQuantity()?.doubleValue(for: unit),
                max: stats.maximumQuantity()?.doubleValue(for: unit)
            )
        }
    }

    // MARK: 送信・保存

    /// サーバーの上限（1回 1,000件）を超えないように分けて送る
    private func upload(_ batch: HealthBatch) async throws {
        guard !batch.isEmpty else { return }
        var pending = batch
        while !pending.isEmpty {
            var part = HealthBatch()
            var room = uploadChunk
            func take<T>(_ items: inout [T]) -> [T] {
                let count = Swift.min(room, items.count)
                let taken = Array(items.prefix(count))
                items.removeFirst(count)
                room -= count
                return taken
            }
            part.samples = take(&pending.samples)
            part.workouts = take(&pending.workouts)
            part.buckets = take(&pending.buckets)
            part.deleted = take(&pending.deleted)
            try await APIClient.shared.uploadHealth(part)
        }
    }

    private func loadAnchor(_ name: String) -> HKQueryAnchor? {
        guard let data = defaults.data(forKey: Keys.anchor(name)) else { return nil }
        return try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: data)
    }

    private func saveAnchor(_ anchor: HKQueryAnchor, name: String) {
        if let data = try? NSKeyedArchiver.archivedData(withRootObject: anchor, requiringSecureCoding: true) {
            defaults.set(data, forKey: Keys.anchor(name))
        }
    }

    // MARK: - 書き込み（カフェイン・飲酒）

    func saveCaffeine(mg: Double, at date: Date, logID: String) async throws {
        try await save(HKQuantityType(.dietaryCaffeine), quantity: HKQuantity(unit: .gramUnit(with: .milli), doubleValue: mg), at: date, logID: logID)
    }

    func saveAlcohol(drinks: Double, at date: Date, logID: String) async throws {
        try await save(HKQuantityType(.numberOfAlcoholicBeverages), quantity: HKQuantity(unit: .count(), doubleValue: drinks), at: date, logID: logID)
    }

    private func save(_ type: HKQuantityType, quantity: HKQuantity, at date: Date, logID: String) async throws {
        guard Self.isAvailable else { return }
        let sample = HKQuantitySample(
            type: type, quantity: quantity, start: date, end: date,
            // 同じ記録を2回書き込まないように、記録の id を付ける
            metadata: [HKMetadataKeySyncIdentifier: logID, HKMetadataKeySyncVersion: 1]
        )
        try await store.save(sample)
    }
}
