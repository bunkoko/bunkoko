import SwiftUI

struct HomeView: View {
    @State private var home: HomeResponse?
    @State private var errorMessage: String?
    @State private var feeling: Int?
    @AppStorage("health.authorizationRequested") private var healthRequested = false

    var body: some View {
        NavigationStack {
            List {
                if let errorMessage {
                    Section { Text(errorMessage).foregroundStyle(.red) }
                }
                if !healthRequested {
                    Section {
                        Label("「設定」で「ヘルスケアへのアクセスを許可」を押してください", systemImage: "heart.text.square")
                    }
                }

                if let night = home?.lastNight {
                    LastNightSection(day: night)
                } else if home != nil {
                    Section("昨夜") { Text("まだ昨夜のデータがありません").foregroundStyle(.secondary) }
                }

                Section("朝の体感（すっきり度）") {
                    FeelingPicker(selected: $feeling)
                }

                if let home {
                    RoomSection(room: home.room, advice: home.roomAdvice)
                    BedtimeSection(bedtime: home.bedtime)
                    if home.mealsWithoutEstimate > 0 {
                        Section {
                            Label("目安がまだの食事が \(home.mealsWithoutEstimate) 件あります（毎朝まとめて出します）", systemImage: "fork.knife")
                                .font(.callout)
                        }
                    }
                }
            }
            .navigationTitle("健康管理")
            .navigationDestination(for: String.self) { date in
                NightDetailView(date: date, day: home?.lastNight?.date == date ? home?.lastNight : nil)
            }
            .task { await load() }
            .refreshable {
                await HealthSync.shared.sync()
                await load()
            }
        }
    }

    private func load() async {
        do {
            home = try await APIClient.shared.home()
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

struct LastNightSection: View {
    let day: DayView

    var body: some View {
        let sleep = day.metrics.sleep
        let room = day.metrics.room
        Section("昨夜（\(Fmt.dateLabel(day.date))）") {
            NavigationLink(value: day.date) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(Fmt.duration(sleep?.asleepMin)).font(.title.bold())
                    Text("\(Fmt.time(sleep?.onset)) 〜 \(Fmt.time(sleep?.wake))")
                        .foregroundStyle(.secondary)
                }
            }
            MetricRow(label: "寝つきまで", value: Fmt.duration(sleep?.latencyMin))
            MetricRow(label: "中途覚醒", value: sleep?.awakenings.map { "\(Int($0))回" } ?? "—")
            MetricRow(label: "睡眠効率", value: Fmt.percent(sleep?.efficiency))
            MetricRow(label: "深い睡眠 / レム睡眠", value: "\(Fmt.duration(sleep?.deepMin)) / \(Fmt.duration(sleep?.remMin))")
            MetricRow(label: "CO2（最高）", value: Fmt.number(room?.co2Max, unit: "ppm"))
            MetricRow(label: "室温（平均）", value: Fmt.number(room?.temperatureAvg, digits: 1, unit: "℃"))
            if day.unusual {
                Label("心拍や体温などが、いつもと違う値でした。続いて気になる場合は医療機関に相談してください。", systemImage: "exclamationmark.circle")
                    .font(.callout)
                    .foregroundStyle(.orange)
            }
            if day.metrics.quality?.usable == false {
                Label("Apple Watch のデータが少なめの夜でした", systemImage: "applewatch.slash")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct FeelingPicker: View {
    @Binding var selected: Int?

    var body: some View {
        HStack {
            ForEach(1...5, id: \.self) { score in
                Button {
                    selected = score
                    Task { await Recorder.morningFeeling(score) }
                } label: {
                    Text("\(score)")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .tint(selected == score ? .accentColor : .gray)
            }
        }
        Text("1 = ぐったり 〜 5 = すっきり").font(.caption).foregroundStyle(.secondary)
    }
}

struct RoomSection: View {
    let room: RoomNow
    let advice: [Advice]

    var body: some View {
        Section("今の部屋") {
            if room.measuredAt == nil {
                Text("30分以内の部屋のデータがありません").foregroundStyle(.secondary)
            } else {
                MetricRow(label: "室温", value: Fmt.number(room.temperature, digits: 1, unit: "℃"))
                MetricRow(label: "湿度", value: Fmt.number(room.humidity, unit: "%"))
                MetricRow(label: "CO2", value: Fmt.number(room.co2, unit: "ppm"))
                if let light = room.ceilingLight {
                    MetricRow(label: "照明", value: light.on ? "\(Fmt.number(light.brightness, unit: "%"))・\(Fmt.number(light.colorTemperature, unit: "K"))" : "消えている")
                }
                if let measured = room.measuredAt {
                    Text("\(Fmt.time(measured)) 時点").font(.caption).foregroundStyle(.secondary)
                }
            }
            ForEach(advice, id: \.self) { item in
                Label(item.message, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
            }
        }
    }
}

struct BedtimeSection: View {
    let bedtime: Bedtime

    var body: some View {
        Section("今夜") {
            let minutes = bedtime.minutesUntil
            MetricRow(label: "目標の就寝時刻", value: Fmt.time(bedtime.target))
            if minutes >= 0 {
                MetricRow(label: "就寝まで", value: Fmt.duration(minutes))
            } else {
                MetricRow(label: "目標から", value: "\(Fmt.duration(-minutes)) 過ぎています")
            }
        }
    }
}

struct MetricRow: View {
    let label: String
    let value: String

    var body: some View {
        HStack {
            Text(label)
            Spacer()
            Text(value).foregroundStyle(.secondary).monospacedDigit()
        }
    }
}
