import SwiftUI

struct HomeView: View {
    @State private var home: HomeResponse?
    @State private var errorMessage: String?
    /// 今朝の体感（入れていなければ nil）
    @State private var feeling: Int?
    /// 体感を入れる前に、昨夜の数字を見ることにした
    @State private var revealNight = false
    /// 入れ忘れていた昨日のふり返りの日（入れ忘れがなければ nil）
    @State private var missingCheckinDay: String?
    /// その日のふり返りを、この画面で入れた
    @State private var savedMissingCheckin = false
    @State private var now = Date()
    @AppStorage("health.authorizationRequested") private var healthRequested = false

    private var hour: Int { Calendar.current.component(.hour, from: now) }
    /// 朝の体感を聞く時間帯（4時〜18時）
    private var isMorning: Bool { hour >= DayKey.rolloverHour && hour < 18 }
    /// 1日のふり返りを聞く時間帯（17時〜翌4時）
    private var isEvening: Bool { hour >= 17 || hour < DayKey.rolloverHour }

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

                // 朝: 昨夜の数字を見る前に、体感を聞く（数字に点数が引っ張られないように）
                if isMorning {
                    Section {
                        FeelingPicker(selected: $feeling)
                    } header: {
                        Text("今朝のすっきり度")
                    } footer: {
                        if feeling == nil {
                            Text("昨夜の数字を見る前に入れると、数字に引っ張られずに記録できます")
                        }
                    }
                }

                if feeling != nil || revealNight || !isMorning {
                    if let night = home?.lastNight {
                        LastNightSection(day: night)
                    } else if home != nil {
                        Section("昨夜") { Text("まだ昨夜のデータがありません").foregroundStyle(.secondary) }
                    }
                } else {
                    Section {
                        Button("先に昨夜の数字を見る") { revealNight = true }
                    }
                }

                // 夜: 今日の日中をふり返る
                if isEvening {
                    Section {
                        DayCheckinForm(day: DayKey.activeDay(now))
                    } header: {
                        Text("今日のふり返り")
                    } footer: {
                        Text("入れた分はすぐに保存されます。どちらも飛ばせます")
                    }
                }

                // 朝: 昨日のふり返りを入れ忘れていたら、ここで入れられる。
                // 調子とストレスは1つずつ保存される。1つ入れても欄を消さずに、続けてもう1つも入れられるようにする
                if isMorning, let day = missingCheckinDay {
                    Section("\(DayKey.label(day))のふり返り（\(savedMissingCheckin ? "保存しました" : "まだ入っていません")）") {
                        DayCheckinForm(day: day) {
                            savedMissingCheckin = true
                        }
                    }
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
        now = Date()
        let today = Fmt.dateString(now)
        feeling = await LogStore.shared.log(id: Recorder.feelingID(day: today))?.data.score
        await findMissingCheckin()
        do {
            home = try await APIClient.shared.home()
            errorMessage = nil
            // 別の iPhone などで入れた体感も、サーバーにあれば使う
            if feeling == nil, let night = home?.lastNight, night.date == today, let score = night.metrics.morning?.feeling {
                feeling = Int(score)
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// 昨日のふり返りを入れ忘れていないか確かめる。
    /// この画面で入れ始めた欄は、そのまま出しておく（調子だけ入れて、ストレスを入れる前に消えないように）
    private func findMissingCheckin() async {
        let yesterday = DayKey.adding(-1, to: DayKey.activeDay(now))
        if savedMissingCheckin, missingCheckinDay == yesterday { return }
        missingCheckinDay = await LogStore.shared.log(id: Recorder.checkinID(day: yesterday)) == nil ? yesterday : nil
        savedMissingCheckin = false
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
        ScoreRow(title: "起きたときの感じ", low: "ぐったり", high: "すっきり", value: $selected) { score in
            Task { await Recorder.morningFeeling(score) }
        }
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
