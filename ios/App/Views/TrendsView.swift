import Charts
import SwiftUI

/// 推移: 日ごとの睡眠と部屋の値のグラフ
struct TrendsView: View {
    @State private var range = 30
    @State private var days: [DayView] = []
    @State private var errorMessage: String?

    private struct DayPoint: Identifiable {
        let date: Date
        let value: Double
        var id: Date { date }
    }

    private func points(_ value: (DayView) -> Double?) -> [DayPoint] {
        days.compactMap { day in
            guard let date = Fmt.parseDate(day.date), let v = value(day) else { return nil }
            return DayPoint(date: date, value: v)
        }
        .sorted { $0.date < $1.date }
    }

    var body: some View {
        NavigationStack {
            List {
                Picker("期間", selection: $range) {
                    Text("2週間").tag(14)
                    Text("30日").tag(30)
                    Text("90日").tag(90)
                }
                .pickerStyle(.segmented)

                if let errorMessage {
                    Text(errorMessage).foregroundStyle(.red)
                }

                // 目的（朝すっきり起きられて、日中の調子がよい）を先に出す
                trendSection("朝の体感（1 ぐったり〜5 すっきり）", points { $0.metrics.morning?.feeling }, color: .mint, domain: 1...5)
                trendSection("日中の調子（1 悪い〜5 よい）", points { $0.metrics.nextDay?.condition }, color: .cyan, domain: 1...5)
                trendSection("ストレス（1 なかった〜5 とても強い）", points { $0.metrics.nextDay?.stress }, color: .red, domain: 1...5)
                trendSection("眠った時間（時間）", points { $0.metrics.sleep?.asleepMin.map { $0 / 60 } }, bar: true, color: .indigo)
                trendSection("睡眠効率（%）", points { $0.metrics.sleep?.efficiency.map { $0 * 100 } }, color: .blue)
                trendSection("寝つきまで（分）", points { $0.metrics.sleep?.latencyMin }, color: .purple)
                trendSection("CO2 の最高（ppm）", points { $0.metrics.room?.co2Max }, color: .green, threshold: 1000)
                trendSection("室温の平均（℃）", points { $0.metrics.room?.temperatureAvg }, color: .orange)
                trendSection("安静時心拍数", points { $0.metrics.body?.restingHeartRate }, color: .pink)
                trendSection("HRV（7日平均, ms）", points { $0.hrv7dAvg }, color: .teal)
            }
            .navigationTitle("推移")
            .task(id: range) { await load() }
            .refreshable { await load() }
        }
    }

    @ViewBuilder
    private func trendSection(_ title: String, _ data: [DayPoint], bar: Bool = false, color: Color, threshold: Double? = nil, domain: ClosedRange<Double>? = nil) -> some View {
        Section(title) {
            if data.isEmpty {
                Text("データなし").foregroundStyle(.secondary)
            } else if let domain {
                // 1〜5 の点数は、縦の範囲をいつも同じにする（日によって目盛りが変わらないように）
                chart(title, data, bar: bar, color: color, threshold: threshold)
                    .chartYScale(domain: domain)
                    .frame(height: 140)
            } else {
                chart(title, data, bar: bar, color: color, threshold: threshold)
                    .frame(height: 140)
            }
        }
    }

    private func chart(_ title: String, _ data: [DayPoint], bar: Bool, color: Color, threshold: Double?) -> some View {
        Chart {
            ForEach(data) { point in
                if bar {
                    BarMark(x: .value("日", point.date, unit: .day), y: .value(title, point.value))
                        .foregroundStyle(color)
                } else {
                    LineMark(x: .value("日", point.date, unit: .day), y: .value(title, point.value))
                        .foregroundStyle(color)
                    PointMark(x: .value("日", point.date, unit: .day), y: .value(title, point.value))
                        .foregroundStyle(color)
                }
            }
            if let threshold {
                RuleMark(y: .value("目安", threshold))
                    .lineStyle(StrokeStyle(lineWidth: 1, dash: [4]))
                    .foregroundStyle(.red)
            }
        }
    }

    private func load() async {
        let to = Fmt.dateString(Date())
        let from = Fmt.dateString(Calendar.current.date(byAdding: .day, value: -(range - 1), to: Date()) ?? Date())
        do {
            days = try await APIClient.shared.days(from: from, to: to)
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
