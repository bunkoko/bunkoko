import Foundation

// サーバーとやり取りするデータの形（docs/phase1-design.md の 3章）。
// サーバーの値は増えたり null になったりするので、数値はほぼすべて Optional にしている。

// MARK: - アプリ → サーバー

struct SourceDTO: Encodable {
    let name: String?
    let productType: String?
}

struct SampleDTO: Encodable {
    let uuid: String
    let type: String
    let start: Int64
    let end: Int64
    let value: Double?
    let valueText: String?
    let unit: String?
    let source: SourceDTO?
}

struct BucketDTO: Encodable {
    let type: String
    let start: Int64
    let minutes: Int
    let sum: Double?
    let avg: Double?
    let min: Double?
    let max: Double?
}

struct WorkoutDTO: Encodable {
    let uuid: String
    let activityType: String
    let start: Int64
    let end: Int64
    let energyKcal: Double?
    let distanceM: Double?
    let avgHeartRate: Double?
    let source: SourceDTO?
}

struct HealthBatch: Encodable {
    var samples: [SampleDTO] = []
    var buckets: [BucketDTO] = []
    var workouts: [WorkoutDTO] = []
    var deleted: [String] = []

    var count: Int { samples.count + buckets.count + workouts.count + deleted.count }
    var isEmpty: Bool { count == 0 }
}

/// 記録の中身。種類ごとに使う項目だけを入れる（nil の項目は送らない）
struct LogData: Codable, Hashable {
    var text: String? = nil
    var amount: String? = nil
    var meal: String? = nil
    var score: Int? = nil
    var note: String? = nil
    var item: String? = nil
    var mg: Double? = nil
    var drinks: Double? = nil
    var state: String? = nil
    var cumulativeMinutes: Int? = nil
}

struct LogDTO: Encodable {
    let id: String
    let kind: String
    let occurredAt: Int64
    let data: LogData
}

struct LogsBatch: Encodable {
    var logs: [LogDTO] = []
    var deleted: [String] = []
}

// MARK: - サーバー → アプリ

struct OKResponse: Decodable {
    let ok: Bool
}

struct CeilingLightNow: Decodable {
    let on: Bool
    let brightness: Double?
    let colorTemperature: Double?
}

struct RoomNow: Decodable {
    let measuredAt: Double?
    let co2: Double?
    let temperature: Double?
    let humidity: Double?
    let lightLevel: Double?
    let inBed: Bool?
    let ceilingLight: CeilingLightNow?
}

struct Advice: Decodable, Hashable {
    let kind: String
    let message: String
}

struct Bedtime: Decodable {
    let target: Double
    let minutesUntil: Double
}

struct HomeResponse: Decodable {
    let room: RoomNow
    let roomAdvice: [Advice]
    let lastNight: DayView?
    let bedtime: Bedtime
    let mealsWithoutEstimate: Int
}

struct SleepSegment: Decodable, Identifiable, Hashable {
    let start: Double
    let end: Double
    let stage: String
    var id: String { "\(start)-\(stage)" }
}

struct SleepMetrics: Decodable {
    let onset: Double
    let wake: Double
    let inBedStart: Double?
    let inBedSource: String?
    let asleepMin: Double?
    let deepMin: Double?
    let remMin: Double?
    let coreMin: Double?
    let awakeMin: Double?
    let awakenings: Double?
    let timeInBedMin: Double?
    let efficiency: Double?
    let latencyMin: Double?
    let segments: [SleepSegment]?
}

struct BodyMetrics: Decodable {
    let hrAvg: Double?
    let hrMin: Double?
    let hrvAvg: Double?
    let respiratoryRateAvg: Double?
    let spo2Avg: Double?
    let spo2Min: Double?
    let wristTemperature: Double?
    let restingHeartRate: Double?
}

struct RoomMetrics: Decodable {
    let co2Avg: Double?
    let co2Max: Double?
    let co2OverMin: Double?
    let co2AtBed: Double?
    let temperatureAvg: Double?
    let temperatureMin: Double?
    let temperatureMax: Double?
    let humidityAvg: Double?
    let lightLevelAvg: Double?
    let brightMin: Double?
    let windowOpenMin: Double?
    let doorOpenMin: Double?
    let purifierMin: Double?
}

struct PreBedMetrics: Decodable {
    let start: Double
    let end: Double
    let heartRateAvg: Double?
    let temperatureAvg: Double?
    let lightOnMin: Double?
    let lightBrightnessAvg: Double?
    let lightColorTemperatureAvg: Double?
    let lightsOffAt: Double?
    let tvOnMin: Double?
    let windowOpenMin: Double?
    let windowOpenings: Double?
    let purifierMin: Double?
}

struct LastMeal: Decodable {
    let kcal: Double?
    let greasiness: Double?
    let spiciness: Double?
    let volume: Double?
    let estimated: Bool
}

struct BehaviorMetrics: Decodable {
    let bathToBedMin: Double?
    let lastMealToBedMin: Double?
    let lastMeal: LastMeal?
    let lateSnack: Bool?
    let eveningKcal: Double?
    let caffeineMg: Double?
    let lastCaffeineToBedMin: Double?
    let alcoholDrinks: Double?
    let lastPhoneUseAt: Double?
    let phoneInBedMin: Double?
    let nightPhoneMin: Double?
    let chargerConnectedAt: Double?
    let mindfulMin: Double?
}

struct DayActivity: Decodable {
    let start: Double
    let end: Double
    let steps: Double?
    let exerciseMin: Double?
    let standMin: Double?
    let activeKcal: Double?
    let daylightMin: Double?
    let morningDaylightMin: Double?
    let workoutMin: Double?
    let lastWorkoutToBedMin: Double?
    let napMin: Double?
}

struct MorningMetrics: Decodable {
    let feeling: Double?
}

struct QualityMetrics: Decodable {
    let hasSleep: Bool
    let heartRateCoverage: Double?
    let roomCoverage: Double?
    let presenceAvailable: Bool?
    let usable: Bool
}

struct DayMetrics: Decodable {
    let sleep: SleepMetrics?
    let body: BodyMetrics?
    let room: RoomMetrics?
    let preBed: PreBedMetrics?
    let behavior: BehaviorMetrics?
    let day: DayActivity?
    let morning: MorningMetrics?
    let quality: QualityMetrics?
}

struct Deviation: Decodable {
    let value: Double
    let median: Double
    let scale: Double
    let z: Double
}

struct DayView: Decodable, Identifiable {
    let date: String
    let metrics: DayMetrics
    let baseline: [String: Deviation?]
    let hrv7dAvg: Double?
    let unusual: Bool
    var id: String { date }

    func deviation(_ key: String) -> Deviation? {
        baseline[key] ?? nil
    }
}

struct DaysResponse: Decodable {
    let days: [DayView]
}

struct TimelineWindow: Decodable {
    let start: Double
    let end: Double
}

struct TimelineSeries: Decodable {
    let co2: [[Double]]
    let temperature: [[Double]]
    let humidity: [[Double]]
    let lightLevel: [[Double]]
    let inBed: [[Double]]
    /// [時刻, 明るさ（消えていれば 0）, 色温度]
    let ceilingLight: [[Double?]]
}

struct TimelineEvent: Decodable, Identifiable, Hashable {
    let t: Double
    let kind: String
    let label: String?
    var id: String { "\(t)-\(kind)" }
}

struct TimelineResponse: Decodable {
    let date: String
    let window: TimelineWindow
    let sleep: SleepMetrics?
    let heartRate: [[Double]]
    let series: TimelineSeries
    let events: [TimelineEvent]
}

struct MealItem: Codable, Hashable {
    var name: String
    var kcal: Double?
}

struct MealEstimate: Codable, Hashable {
    var kcal: Double
    var proteinG: Double?
    var fatG: Double?
    var carbsG: Double?
    var caffeineMg: Double
    var alcoholDrinks: Double
    var greasiness: Int
    var spiciness: Int
    var volume: Int
    var confidence: String
    var items: [MealItem]
}

struct Meal: Decodable, Identifiable {
    let id: String
    let occurredAt: Double
    let text: String
    let amount: String?
    let meal: String?
    let estimate: MealEstimate?
    let estimateSource: String?
}

struct MealsResponse: Decodable {
    let meals: [Meal]
}

struct NotificationSettings: Codable {
    var bedtime: Bool
    var bedtimeLeadMinutes: Int
    var morningReport: Bool
}

struct ComfortSettings: Codable {
    var co2Max: Double
    var humidityMin: Double
    var humidityMax: Double
    var temperatureMin: Double?
    var temperatureMax: Double?
}

struct ServerSettings: Codable {
    var timeZone: String
    var targetBedtime: String
    var targetWake: String
    var notifications: NotificationSettings
    var comfort: ComfortSettings
    var deviceRoles: [String: String]
}

struct SettingsResponse: Decodable {
    let settings: ServerSettings
}

struct APNsRegistration: Encodable {
    let token: String
    let environment: String
}

struct EstimateUpdate: Encodable {
    let estimate: MealEstimate
}
