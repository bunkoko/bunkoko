import SwiftUI

/// 1〜5 から1つ選ぶ行。両端の意味を下に出す
struct ScoreRow: View {
    let title: String
    let low: String
    let high: String
    @Binding var value: Int?
    var onSelect: (Int) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.subheadline.bold())
            HStack {
                ForEach(1...5, id: \.self) { score in
                    Button {
                        value = score
                        onSelect(score)
                    } label: {
                        Text("\(score)").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    .tint(value == score ? .accentColor : .gray)
                    .accessibilityLabel("\(title) \(score)")
                }
            }
            HStack {
                Text("1 = \(low)")
                Spacer()
                Text("5 = \(high)")
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
    }
}

/// 1日のふり返り（今日の調子・日中の眠気・ストレス）。押すたびに保存する（どの項目も飛ばせる）
struct DayCheckinForm: View {
    let day: String
    var onSaved: () -> Void = {}

    @State private var condition: Int?
    @State private var sleepiness: Int?
    @State private var stress: Int?

    var body: some View {
        let word = DayKey.label(day)
        VStack(spacing: 12) {
            ScoreRow(title: "\(word)の調子", low: "悪い", high: "よい", value: $condition) { _ in save() }
            Divider()
            ScoreRow(title: "日中の眠気", low: "なかった", high: "とても眠かった", value: $sleepiness) { _ in save() }
            Divider()
            ScoreRow(title: "ストレス", low: "なかった", high: "とても強かった", value: $stress) { _ in save() }
        }
        .task(id: day) { await load() }
    }

    private func load() async {
        let data = await LogStore.shared.log(id: Recorder.checkinID(day: day))?.data
        condition = data?.condition
        sleepiness = data?.sleepiness
        stress = data?.stress
    }

    private func save() {
        let values = (condition, sleepiness, stress)
        Task {
            await Recorder.dayCheckin(day: day, condition: values.0, sleepiness: values.1, stress: values.2)
            onSaved()
        }
    }
}
