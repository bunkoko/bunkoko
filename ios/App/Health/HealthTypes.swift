import Foundation
import HealthKit

/// 読み取る HealthKit のデータと、サーバーに送るときの形（docs/analysis.md の 2.1・2.4）。
/// 病気の判定に関わるデータ（心電図・心房細動・不規則な心拍の通知など）は読まない。
enum HealthTypes {
    static let bpm = HKUnit.count().unitDivided(by: .minute())

    /// 測った値そのまま送るデータ（差分を取る）
    struct SampleSpec {
        let type: HKSampleType
        /// サーバーでの名前
        let name: String
        let unit: HKUnit?
        var scale: Double = 1
        /// このアプリが書き込んだ記録は送らない（記録の方で送っているため）
        var skipOwnSamples = false
    }

    static let anchored: [SampleSpec] = [
        SampleSpec(type: HKCategoryType(.sleepAnalysis), name: "sleepAnalysis", unit: nil),
        SampleSpec(type: HKQuantityType(.heartRateVariabilitySDNN), name: "heartRateVariabilitySDNN", unit: .secondUnit(with: .milli)),
        SampleSpec(type: HKQuantityType(.restingHeartRate), name: "restingHeartRate", unit: bpm),
        SampleSpec(type: HKQuantityType(.walkingHeartRateAverage), name: "walkingHeartRateAverage", unit: bpm),
        SampleSpec(type: HKQuantityType(.heartRateRecoveryOneMinute), name: "heartRateRecoveryOneMinute", unit: bpm),
        SampleSpec(type: HKQuantityType(.respiratoryRate), name: "respiratoryRate", unit: bpm),
        SampleSpec(type: HKQuantityType(.oxygenSaturation), name: "oxygenSaturation", unit: .percent(), scale: 100),
        SampleSpec(type: HKQuantityType(.appleSleepingWristTemperature), name: "appleSleepingWristTemperature", unit: .degreeCelsius()),
        SampleSpec(type: HKQuantityType(.appleSleepingBreathingDisturbances), name: "appleSleepingBreathingDisturbances", unit: .count()),
        SampleSpec(type: HKQuantityType(.vo2Max), name: "vo2Max", unit: HKUnit(from: "ml/kg*min")),
        SampleSpec(type: HKQuantityType(.dietaryCaffeine), name: "dietaryCaffeine", unit: .gramUnit(with: .milli), skipOwnSamples: true),
        SampleSpec(type: HKQuantityType(.numberOfAlcoholicBeverages), name: "numberOfAlcoholicBeverages", unit: .count(), skipOwnSamples: true),
        SampleSpec(type: HKCategoryType(.mindfulSession), name: "mindfulSession", unit: nil),
        SampleSpec(type: HKObjectType.stateOfMindType(), name: "stateOfMind", unit: nil),
        SampleSpec(type: HKObjectType.workoutType(), name: "workout", unit: nil),
    ]

    /// 一定の時間ごとに集計して送るデータ
    struct BucketSpec {
        let identifier: HKQuantityTypeIdentifier
        let name: String
        let minutes: Int
        let options: HKStatisticsOptions
        let unit: HKUnit
    }

    /// 心拍数は、夜の詳細画面で部屋の値と重ねるため5分ごと
    static let heartRate = BucketSpec(
        identifier: .heartRate, name: "heartRate", minutes: 5,
        options: [.discreteAverage, .discreteMin, .discreteMax], unit: bpm
    )

    static let hourly: [BucketSpec] = [
        BucketSpec(identifier: .stepCount, name: "stepCount", minutes: 60, options: .cumulativeSum, unit: .count()),
        BucketSpec(identifier: .distanceWalkingRunning, name: "distanceWalkingRunning", minutes: 60, options: .cumulativeSum, unit: .meter()),
        BucketSpec(identifier: .flightsClimbed, name: "flightsClimbed", minutes: 60, options: .cumulativeSum, unit: .count()),
        BucketSpec(identifier: .activeEnergyBurned, name: "activeEnergyBurned", minutes: 60, options: .cumulativeSum, unit: .kilocalorie()),
        BucketSpec(identifier: .appleExerciseTime, name: "appleExerciseTime", minutes: 60, options: .cumulativeSum, unit: .minute()),
        BucketSpec(identifier: .appleStandTime, name: "appleStandTime", minutes: 60, options: .cumulativeSum, unit: .minute()),
        BucketSpec(identifier: .timeInDaylight, name: "timeInDaylight", minutes: 60, options: .cumulativeSum, unit: .minute()),
        BucketSpec(identifier: .environmentalAudioExposure, name: "environmentalAudioExposure", minutes: 60, options: .discreteAverage, unit: .decibelAWeightedSoundPressureLevel()),
        BucketSpec(identifier: .headphoneAudioExposure, name: "headphoneAudioExposure", minutes: 60, options: .discreteAverage, unit: .decibelAWeightedSoundPressureLevel()),
    ]

    static var readTypes: Set<HKObjectType> {
        var types = Set<HKObjectType>(anchored.map { $0.type as HKObjectType })
        types.insert(HKQuantityType(heartRate.identifier))
        for spec in hourly { types.insert(HKQuantityType(spec.identifier)) }
        // ワークアウトのカロリー・距離・心拍の集計に使う
        types.insert(HKQuantityType(.activeEnergyBurned))
        types.insert(HKQuantityType(.distanceWalkingRunning))
        return types
    }

    /// 記録したカフェインと飲酒は、ヘルスケアにも書き込む
    static let writeTypes: Set<HKSampleType> = [
        HKQuantityType(.dietaryCaffeine),
        HKQuantityType(.numberOfAlcoholicBeverages),
    ]

    /// 新しいデータが記録されたら、iPhone がアプリを起こす。睡眠とワークアウトはすぐ、そのほかは1時間ごと
    static let observed: [(HKSampleType, HKUpdateFrequency)] = [
        (HKCategoryType(.sleepAnalysis), .immediate),
        (HKObjectType.workoutType(), .immediate),
        (HKQuantityType(.heartRate), .hourly),
        (HKQuantityType(.heartRateVariabilitySDNN), .hourly),
        (HKQuantityType(.stepCount), .hourly),
    ]

    static func sleepStageName(_ value: Int) -> String? {
        guard let stage = HKCategoryValueSleepAnalysis(rawValue: value) else { return nil }
        switch stage {
        case .inBed: return "inBed"
        case .asleepUnspecified: return "asleepUnspecified"
        case .awake: return "awake"
        case .asleepCore: return "asleepCore"
        case .asleepDeep: return "asleepDeep"
        case .asleepREM: return "asleepREM"
        @unknown default: return nil
        }
    }

    /// サーバーで使えるのは英数字と _ だけなので、ワークアウトの種類を名前にする
    static func workoutName(_ type: HKWorkoutActivityType) -> String {
        switch type {
        case .running: return "running"
        case .walking: return "walking"
        case .cycling: return "cycling"
        case .swimming: return "swimming"
        case .hiking: return "hiking"
        case .yoga: return "yoga"
        case .traditionalStrengthTraining: return "strengthTraining"
        case .functionalStrengthTraining: return "functionalStrengthTraining"
        case .highIntensityIntervalTraining: return "hiit"
        case .coreTraining: return "coreTraining"
        case .elliptical: return "elliptical"
        case .rowing: return "rowing"
        case .dance: return "dance"
        case .mindAndBody: return "mindAndBody"
        case .other: return "other"
        default: return "type_\(type.rawValue)"
        }
    }
}
