import Charts
import SwiftUI

/// グラフに使う「時刻と値」
struct TimePoint: Identifiable {
    let date: Date
    let value: Double
    var id: Date { date }
}

extension Array where Element == [Double] {
    var timePoints: [TimePoint] {
        compactMap { pair in pair.count >= 2 ? TimePoint(date: Date(epochMs: pair[0]), value: pair[1]) : nil }
    }
}

enum SleepStage {
    static func label(_ stage: String) -> String {
        switch stage {
        case "asleepDeep": return "深い"
        case "asleepCore": return "コア"
        case "asleepREM": return "レム"
        case "awake": return "覚醒"
        default: return "睡眠"
        }
    }

    static let order = ["覚醒", "レム", "コア", "深い", "睡眠"]
}

// MARK: - 夜の一覧

struct NightListView: View {
    @State private var days: [DayView] = []
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            List {
                if let errorMessage {
                    Text(errorMessage).foregroundStyle(.red)
                }
                ForEach(days) { day in
                    NavigationLink(value: day.date) {
                        NightRow(day: day)
                    }
                }
            }
            .navigationTitle("夜")
            .navigationDestination(for: String.self) { date in
                NightDetailView(date: date, day: days.first { $0.date == date })
            }
            .task { await load() }
            .refreshable { await load() }
        }
    }

    private func load() async {
        let to = Fmt.dateString(Date())
        let from = Fmt.dateString(Calendar.current.date(byAdding: .day, value: -29, to: Date()) ?? Date())
        do {
            days = try await APIClient.shared.days(from: from, to: to)
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

struct NightRow: View {
    let day: DayView

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(Fmt.dateLabel(day.date)).font(.headline)
                Spacer()
                if day.unusual { Image(systemName: "exclamationmark.circle").foregroundStyle(.orange) }
                if day.metrics.quality?.usable == false { Image(systemName: "applewatch.slash").foregroundStyle(.secondary) }
            }
            if let sleep = day.metrics.sleep {
                Text("\(Fmt.duration(sleep.asleepMin))（効率 \(Fmt.percent(sleep.efficiency))・寝つき \(Fmt.duration(sleep.latencyMin))）")
                    .font(.subheadline)
                Text("CO2 最高 \(Fmt.number(day.metrics.room?.co2Max, unit: "ppm"))・室温 \(Fmt.number(day.metrics.room?.temperatureAvg, digits: 1, unit: "℃"))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                Text("睡眠のデータなし").font(.subheadline).foregroundStyle(.secondary)
            }
        }
    }
}

// MARK: - 夜の詳細

struct NightDetailView: View {
    let date: String
    let day: DayView?
    @State private var timeline: TimelineResponse?
    @State private var errorMessage: String?

    var body: some View {
        List {
            if let errorMessage {
                Text(errorMessage).foregroundStyle(.red)
            }
            if let timeline {
                let domain = Date(epochMs: timeline.window.start)...Date(epochMs: timeline.window.end)
                Section("睡眠と体") {
                    StageChart(segments: timeline.sleep?.segments ?? [], domain: domain)
                    SeriesChart(title: "心拍数", unit: "回/分", points: timeline.heartRate.timePoints, domain: domain, color: .pink)
                }
                Section("部屋") {
                    SeriesChart(title: "CO2", unit: "ppm", points: timeline.series.co2.timePoints, domain: domain, threshold: 1000, color: .green)
                    SeriesChart(title: "室温", unit: "℃", points: timeline.series.temperature.timePoints, domain: domain, color: .orange)
                    SeriesChart(title: "湿度", unit: "%", points: timeline.series.humidity.timePoints, domain: domain, color: .teal)
                    SeriesChart(title: "明るさ（枕元）", unit: "1〜20", points: timeline.series.lightLevel.timePoints, domain: domain, color: .yellow)
                }
                if !timeline.events.isEmpty {
                    Section("出来事") {
                        ForEach(timeline.events) { event in
                            HStack {
                                Text(Fmt.time(event.t)).monospacedDigit().foregroundStyle(.secondary)
                                Text(EventText.describe(event))
                            }
                        }
                    }
                }
            }
            if let day {
                DaySummarySections(day: day)
            }
        }
        .navigationTitle(Fmt.dateLabel(date))
        .task { await load() }
    }

    private func load() async {
        do {
            timeline = try await APIClient.shared.timeline(date: date)
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

enum EventText {
    static func describe(_ event: TimelineEvent) -> String {
        switch event.kind {
        case "meal": return "食事: \(event.label ?? "")"
        case "bath": return "入浴"
        case "caffeine": return "カフェイン: \(event.label ?? "")"
        case "alcohol": return "お酒: \(event.label ?? "")"
        case "charger": return event.label == "connected" ? "スマホを充電器につないだ" : "スマホを充電器から外した"
        case "phoneLastUse": return "スマホを最後に使った（目安）"
        case "lightsOff": return "照明を消した"
        case "windowOpen": return "窓を開けた"
        case "windowClose": return "窓を閉めた"
        case "doorOpen": return "ドアを開けた"
        case "doorClose": return "ドアを閉めた"
        default: return event.kind
        }
    }
}

struct StageChart: View {
    let segments: [SleepSegment]
    let domain: ClosedRange<Date>

    var body: some View {
        VStack(alignment: .leading) {
            Text("睡眠ステージ").font(.subheadline.bold())
            if segments.isEmpty {
                Text("データなし").font(.caption).foregroundStyle(.secondary)
            } else {
                Chart(segments) { segment in
                    RectangleMark(
                        xStart: .value("開始", Date(epochMs: segment.start)),
                        xEnd: .value("終了", Date(epochMs: segment.end)),
                        y: .value("段階", SleepStage.label(segment.stage))
                    )
                    .foregroundStyle(by: .value("段階", SleepStage.label(segment.stage)))
                }
                .chartXScale(domain: domain)
                .chartYScale(domain: SleepStage.order)
                .chartLegend(.hidden)
                .frame(height: 150)
            }
        }
    }
}

struct SeriesChart: View {
    let title: String
    let unit: String
    let points: [TimePoint]
    let domain: ClosedRange<Date>
    var threshold: Double? = nil
    var color: Color = .blue

    var body: some View {
        VStack(alignment: .leading) {
            Text("\(title)（\(unit)）").font(.subheadline.bold())
            if points.isEmpty {
                Text("データなし").font(.caption).foregroundStyle(.secondary)
            } else {
                Chart {
                    ForEach(points) { point in
                        LineMark(x: .value("時刻", point.date), y: .value(title, point.value))
                            .foregroundStyle(color)
                    }
                    if let threshold {
                        RuleMark(y: .value("目安", threshold))
                            .lineStyle(StrokeStyle(lineWidth: 1, dash: [4]))
                            .foregroundStyle(.red)
                    }
                }
                .chartXScale(domain: domain)
                .frame(height: 120)
            }
        }
    }
}

/// 1日 = 1行の表の値を、区分ごとに並べる
struct DaySummarySections: View {
    let day: DayView

    var body: some View {
        let m = day.metrics
        Section("睡眠") {
            MetricRow(label: "布団に入った", value: Fmt.time(m.sleep?.inBedStart))
            MetricRow(label: "寝ついた / 起きた", value: "\(Fmt.time(m.sleep?.onset)) / \(Fmt.time(m.sleep?.wake))")
            MetricRow(label: "眠った時間", value: Fmt.duration(m.sleep?.asleepMin))
            MetricRow(label: "深い / レム / コア", value: "\(Fmt.duration(m.sleep?.deepMin)) / \(Fmt.duration(m.sleep?.remMin)) / \(Fmt.duration(m.sleep?.coreMin))")
            MetricRow(label: "中途覚醒", value: "\(Fmt.number(m.sleep?.awakenings, unit: "回"))（\(Fmt.duration(m.sleep?.awakeMin))）")
        }
        Section("体（睡眠中）") {
            MetricRow(label: "心拍数（平均 / 最低）", value: "\(Fmt.number(m.body?.hrAvg)) / \(Fmt.number(m.body?.hrMin))")
            MetricRow(label: "HRV（平均）", value: Fmt.number(m.body?.hrvAvg, unit: "ms"))
            MetricRow(label: "呼吸数", value: Fmt.number(m.body?.respiratoryRateAvg, digits: 1, unit: "回/分"))
            MetricRow(label: "手首皮膚温", value: Fmt.number(m.body?.wristTemperature, digits: 2, unit: "℃"))
            MetricRow(label: "安静時心拍数（その日）", value: Fmt.number(m.body?.restingHeartRate))
            if let z = day.deviation("restingHeartRate")?.z {
                MetricRow(label: "安静時心拍数のいつもとの差", value: Fmt.number(z, digits: 1))
            }
        }
        Section("寝る前（2時間）") {
            MetricRow(label: "照明がついていた", value: Fmt.duration(m.preBed?.lightOnMin))
            MetricRow(label: "照明の明るさ / 色温度", value: "\(Fmt.number(m.preBed?.lightBrightnessAvg, unit: "%")) / \(Fmt.number(m.preBed?.lightColorTemperatureAvg, unit: "K"))")
            MetricRow(label: "消灯", value: Fmt.time(m.preBed?.lightsOffAt))
            MetricRow(label: "テレビ", value: Fmt.duration(m.preBed?.tvOnMin))
            MetricRow(label: "布団に入ったときの CO2", value: Fmt.number(m.room?.co2AtBed, unit: "ppm"))
        }
        Section("行動") {
            MetricRow(label: "入浴から就寝まで", value: Fmt.duration(m.behavior?.bathToBedMin))
            MetricRow(label: "最後の食事から就寝まで", value: Fmt.duration(m.behavior?.lastMealToBedMin))
            MetricRow(label: "カフェイン", value: Fmt.number(m.behavior?.caffeineMg, unit: "mg"))
            MetricRow(label: "お酒", value: Fmt.number(m.behavior?.alcoholDrinks, digits: 1, unit: "杯"))
            MetricRow(label: "スマホを最後に使った", value: Fmt.time(m.behavior?.lastPhoneUseAt))
            MetricRow(label: "布団の中でスマホ", value: Fmt.duration(m.behavior?.phoneInBedMin))
        }
        Section("日中") {
            MetricRow(label: "歩数", value: Fmt.number(m.day?.steps, unit: "歩"))
            MetricRow(label: "日光（合計 / 午前）", value: "\(Fmt.duration(m.day?.daylightMin)) / \(Fmt.duration(m.day?.morningDaylightMin))")
            MetricRow(label: "ワークアウト", value: Fmt.duration(m.day?.workoutMin))
            MetricRow(label: "昼寝", value: Fmt.duration(m.day?.napMin))
        }
    }
}
