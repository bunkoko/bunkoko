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

/// 1日のふり返り（今日の調子・ストレス）。押すたびに保存する（どちらも飛ばせる）
struct DayCheckinForm: View {
    let day: String
    var onSaved: () -> Void = {}

    @State private var condition: Int?
    @State private var stress: Int?
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        let word = DayKey.label(day)
        VStack(spacing: 12) {
            ScoreRow(title: "\(word)の調子", low: "悪い", high: "よい", value: $condition) { score in save(condition: score) }
            Divider()
            ScoreRow(title: "ストレス", low: "なかった", high: "とても強かった", value: $stress) { score in save(stress: score) }
        }
        .task(id: day) { await load() }
        // 通知のボタン（Apple Watch など）で入れた分も、アプリに戻ったときに出す
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { await load() } }
        }
    }

    private func load() async {
        let data = await LogStore.shared.log(id: Recorder.checkinID(day: day))?.data
        condition = data?.condition
        stress = data?.stress
    }

    /// 押した項目だけを保存する（画面に古い値が残っていても、通知で入れたもう片方を消さない）
    private func save(condition: Int? = nil, stress: Int? = nil) {
        Task {
            await Recorder.dayCheckin(day: day, condition: condition, stress: stress)
            onSaved()
        }
    }
}
