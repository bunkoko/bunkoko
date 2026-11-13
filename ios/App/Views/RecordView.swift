import SwiftUI

/// ワンタップで記録する飲み物（目安は ai/meal-estimation-task.md と同じ）
struct QuickDrink: Identifiable {
    enum Kind { case caffeine, alcohol }
    let id: String
    let label: String
    let kind: Kind
    let amount: Double
    let symbol: String

    static let all: [QuickDrink] = [
        QuickDrink(id: "coffee", label: "コーヒー（カフェイン 90mg）", kind: .caffeine, amount: 90, symbol: "cup.and.saucer"),
        QuickDrink(id: "tea", label: "紅茶（30mg）", kind: .caffeine, amount: 30, symbol: "cup.and.saucer.fill"),
        QuickDrink(id: "greentea", label: "緑茶（20mg）", kind: .caffeine, amount: 20, symbol: "leaf"),
        QuickDrink(id: "energy", label: "エナジードリンク（80mg）", kind: .caffeine, amount: 80, symbol: "bolt"),
        QuickDrink(id: "beer", label: "ビール 350ml（1杯）", kind: .alcohol, amount: 1, symbol: "mug"),
        QuickDrink(id: "chuhai", label: "チューハイ 350ml（1杯）", kind: .alcohol, amount: 1, symbol: "takeoutbag.and.cup.and.straw"),
        QuickDrink(id: "wine", label: "ワイン 1杯", kind: .alcohol, amount: 1, symbol: "wineglass"),
        QuickDrink(id: "sake", label: "日本酒 1合（1杯）", kind: .alcohol, amount: 1, symbol: "drop"),
    ]

    /// 記録の品名（ラベルのかっこの前）
    var item: String {
        label.components(separatedBy: "（").first?.trimmingCharacters(in: .whitespaces) ?? label
    }
}

struct RecordView: View {
    @State private var mealText = ""
    @State private var amount = "normal"
    @State private var mealType: String?
    @State private var mealDate = Date()
    @State private var meals: [Meal] = []
    @State private var message: String?
    @State private var editing: Meal?

    var body: some View {
        NavigationStack {
            Form {
                Section("食事") {
                    TextField("料理名（例: 豚骨ラーメン、餃子6個）", text: $mealText, axis: .vertical)
                    Picker("量", selection: $amount) {
                        Text("少なめ").tag("small")
                        Text("ふつう").tag("normal")
                        Text("多め").tag("large")
                    }
                    .pickerStyle(.segmented)
                    Picker("食事の種類", selection: $mealType) {
                        Text("指定なし").tag(String?.none)
                        Text("朝食").tag(String?.some("breakfast"))
                        Text("昼食").tag(String?.some("lunch"))
                        Text("夕食").tag(String?.some("dinner"))
                        Text("間食・夜食").tag(String?.some("snack"))
                    }
                    DatePicker("食べた時刻", selection: $mealDate)
                    Button("記録する") {
                        Task { await recordMeal() }
                    }
                    .disabled(mealText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }

                Section("ワンタップで記録（今の時刻）") {
                    Button {
                        Task {
                            await Recorder.bath()
                            message = "入浴を記録しました（\(Fmt.time(Date()))）"
                        }
                    } label: {
                        Label("入浴した", systemImage: "bathtub")
                    }
                    ForEach(QuickDrink.all) { drink in
                        Button {
                            Task { await record(drink) }
                        } label: {
                            Label(drink.label, systemImage: drink.symbol)
                        }
                    }
                }

                if let message {
                    Section { Text(message).foregroundStyle(.secondary) }
                }

                Section("最近の食事と目安") {
                    if meals.isEmpty {
                        Text("まだ記録がありません").foregroundStyle(.secondary)
                    }
                    ForEach(meals) { meal in
                        Button {
                            editing = meal
                        } label: {
                            MealRow(meal: meal)
                        }
                        .foregroundStyle(.primary)
                    }
                }
            }
            .navigationTitle("記録")
            .task { await loadMeals() }
            .refreshable { await loadMeals() }
            .sheet(item: $editing) { meal in
                EstimateEditor(meal: meal) {
                    await loadMeals()
                }
            }
        }
    }

    private func recordMeal() async {
        let text = mealText.trimmingCharacters(in: .whitespacesAndNewlines)
        await Recorder.meal(text: text, amount: amount, meal: mealType, at: mealDate)
        message = "「\(text)」を記録しました。目安は毎朝まとめて出します"
        mealText = ""
        mealDate = Date()
        await loadMeals()
    }

    private func record(_ drink: QuickDrink) async {
        switch drink.kind {
        case .caffeine: await Recorder.caffeine(item: drink.item, mg: drink.amount)
        case .alcohol: await Recorder.alcohol(item: drink.item, drinks: drink.amount)
        }
        message = "\(drink.item)を記録しました（\(Fmt.time(Date()))）"
    }

    private func loadMeals() async {
        let to = Fmt.dateString(Date())
        let from = Fmt.dateString(Calendar.current.date(byAdding: .day, value: -6, to: Date()) ?? Date())
        meals = (try? await APIClient.shared.meals(from: from, to: to)) ?? meals
    }
}

struct MealRow: View {
    let meal: Meal

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(meal.text).lineLimit(2)
                Spacer()
                Text(Fmt.time(meal.occurredAt)).foregroundStyle(.secondary).monospacedDigit()
            }
            if let estimate = meal.estimate {
                Text("約\(Fmt.number(estimate.kcal))kcal・\(Level.greasiness(estimate.greasiness))・\(Level.volume(estimate.volume))\(meal.estimateSource == "user" ? "（直した値）" : "")")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                Text("目安はまだです（毎朝まとめて出します）").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

enum Level {
    static func greasiness(_ value: Int) -> String { ["", "あっさり", "ふつう", "脂っこい"][max(0, min(3, value))] }
    static func spiciness(_ value: Int) -> String { ["", "辛くない", "ピリ辛", "辛い"][max(0, min(3, value))] }
    static func volume(_ value: Int) -> String { ["", "軽め", "ふつうの量", "多め"][max(0, min(3, value))] }
}

/// 食事の目安を直す画面（直した値は、生成AI の目安より優先される）
struct EstimateEditor: View {
    let meal: Meal
    var onSaved: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var estimate: MealEstimate
    @State private var saving = false
    @State private var errorMessage: String?

    init(meal: Meal, onSaved: @escaping () async -> Void) {
        self.meal = meal
        self.onSaved = onSaved
        _estimate = State(initialValue: meal.estimate ?? MealEstimate(
            kcal: 600, proteinG: nil, fatG: nil, carbsG: nil, caffeineMg: 0, alcoholDrinks: 0,
            greasiness: 2, spiciness: 1, volume: 2, confidence: "low", items: []
        ))
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("食事") {
                    Text(meal.text)
                    if !estimate.items.isEmpty {
                        ForEach(estimate.items, id: \.self) { item in
                            MetricRow(label: item.name, value: Fmt.number(item.kcal, unit: "kcal"))
                        }
                    }
                }
                Section("目安") {
                    HStack {
                        Text("カロリー")
                        TextField("kcal", value: $estimate.kcal, format: .number)
                            .keyboardType(.numberPad)
                            .multilineTextAlignment(.trailing)
                        Text("kcal")
                    }
                    Picker("脂っこさ", selection: $estimate.greasiness) {
                        ForEach(1...3, id: \.self) { Text(Level.greasiness($0)).tag($0) }
                    }
                    Picker("辛さ", selection: $estimate.spiciness) {
                        ForEach(1...3, id: \.self) { Text(Level.spiciness($0)).tag($0) }
                    }
                    Picker("量", selection: $estimate.volume) {
                        ForEach(1...3, id: \.self) { Text(Level.volume($0)).tag($0) }
                    }
                    HStack {
                        Text("カフェイン")
                        TextField("mg", value: $estimate.caffeineMg, format: .number)
                            .keyboardType(.numberPad)
                            .multilineTextAlignment(.trailing)
                        Text("mg")
                    }
                    Stepper("お酒 \(Fmt.number(estimate.alcoholDrinks, digits: 1))杯", value: $estimate.alcoholDrinks, in: 0...20, step: 0.5)
                }
                if let errorMessage {
                    Section { Text(errorMessage).foregroundStyle(.red) }
                }
            }
            .navigationTitle("目安を直す")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("やめる") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "保存中…" : "保存") {
                        Task { await save() }
                    }
                    .disabled(saving)
                }
            }
        }
    }

    private func save() async {
        saving = true
        defer { saving = false }
        do {
            var corrected = estimate
            corrected.confidence = "high"
            try await APIClient.shared.updateEstimate(mealID: meal.id, estimate: corrected)
            await onSaved()
            dismiss()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
