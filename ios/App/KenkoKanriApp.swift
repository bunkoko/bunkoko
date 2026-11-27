import SwiftUI
import UIKit
import UserNotifications

@main
struct KenkoKanriApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView()
        }
        .onChange(of: scenePhase) { _, phase in
            // アプリを開くたびに、新しいデータを送る
            if phase == .active {
                Task { await HealthSync.shared.sync() }
                // 記録のリマインダーを、先の7日分予約し直す
                Task { await Reminders.refresh() }
            }
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        // 朝の体感・夜のふり返りの通知のボタン（アプリが閉じていても、ボタンの答えを受け取れるように、起動するたびに登録する）
        UNUserNotificationCenter.current().setNotificationCategories(Reminders.categories)
        // バックグラウンドで新しい HealthKit のデータを受け取れるように、起動するたびに登録する
        Task { await HealthSync.shared.startBackgroundDelivery() }
        // 通知を許可済みなら、通知の送り先（デバイストークン）をサーバーに登録し直す
        guard AppConfig.hasPaidFeatures else { return true }
        UNUserNotificationCenter.current().getNotificationSettings { settings in
            guard settings.authorizationStatus == .authorized else { return }
            DispatchQueue.main.async { application.registerForRemoteNotifications() }
        }
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        Task { try? await APIClient.shared.registerAPNs(token: token) }
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        print("通知の登録に失敗: \(error.localizedDescription)")
    }

    // 通知のボタンの答え（Apple Watch で押した分も、iPhone のアプリが受け取る）
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        await Reminders.handle(response)
    }

    // アプリを開いているときにも、通知をバナーで出す
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }
}

/// 通知の許可を求める。許可されたら、記録のリマインダーを予約し、
/// Apple Developer Program の機能があれば、サーバーからの通知の送り先も登録する
enum NotificationPermission {
    @MainActor
    static func request() async -> Bool {
        let granted = (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])) ?? false
        if granted {
            if AppConfig.hasPaidFeatures { UIApplication.shared.registerForRemoteNotifications() }
            await Reminders.refresh()
        }
        return granted
    }
}
