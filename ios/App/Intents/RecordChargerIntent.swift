import AppIntents

/// 充電器の状態（ショートカットで選ぶ）
enum ChargerState: String, AppEnum {
    case connected
    case disconnected

    static let typeDisplayRepresentation = TypeDisplayRepresentation(name: "充電の状態")
    static let caseDisplayRepresentations: [ChargerState: DisplayRepresentation] = [
        .connected: "つないだ",
        .disconnected: "外した",
    ]
}

/// ショートカットの「オートメーション」（充電器に接続したとき・外したとき）から呼ぶ操作。
/// 寝る前にスマホを置いた時刻と、朝に手に取った時刻の目安になる。
struct RecordChargerIntent: AppIntent {
    static let title: LocalizedStringResource = "充電を記録"
    static let description = IntentDescription("充電器につないだ・外した時刻を記録します。")
    static let openAppWhenRun = false

    @Parameter(title: "状態")
    var state: ChargerState

    init() {}

    init(state: ChargerState) {
        self.state = state
    }

    func perform() async throws -> some IntentResult {
        await Recorder.charger(connected: state == .connected)
        return .result()
    }
}

struct KenkoKanriShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: RecordChargerIntent(),
            phrases: ["\(.applicationName)で充電を記録"],
            shortTitle: "充電を記録",
            systemImageName: "battery.100percent.bolt"
        )
    }
}
