import DeviceActivity
import Foundation

/// スクリーンタイムの節目を受け取る拡張機能。
/// 夜の時間帯（20時〜翌5時）に、スマホの使用時間の合計が5分増えるたびに iOS から呼ばれる。
/// 節目の名前は "usage-35" のように、その時点の合計の分を表す（ScreenTimeManager が登録する）。
final class ActivityMonitorExtension: DeviceActivityMonitor {
    override func eventDidReachThreshold(_ event: DeviceActivityEvent.Name, activity: DeviceActivityName) {
        super.eventDidReachThreshold(event, activity: activity)
        let name = event.rawValue
        guard name.hasPrefix("usage-"), let minutes = Int(name.dropFirst("usage-".count)) else { return }
        SharedStore.appendScreenEvent(.init(at: Date(), cumulativeMinutes: minutes))
    }
}
