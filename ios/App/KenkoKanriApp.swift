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
            }
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
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

    // アプリを開いているときにも、通知をバナーで出す
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }
}

/// 通知の許可を求め、許可されたら送り先を登録する
enum NotificationPermission {
    @MainActor
    static func request() async -> Bool {
        let granted = (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])) ?? false
        if granted { UIApplication.shared.registerForRemoteNotifications() }
        return granted
    }
}
