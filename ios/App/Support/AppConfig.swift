import Foundation
import Security

/// サーバーの URL と合言葉（APP_TOKEN）。合言葉はキーチェーンに保存する。
enum AppConfig {
    private static let urlKey = "serverURL"
    private static let tokenKey = "appToken"

    static var serverURL: URL? {
        get { UserDefaults.standard.string(forKey: urlKey).flatMap { URL(string: $0) } }
        set { UserDefaults.standard.set(newValue?.absoluteString, forKey: urlKey) }
    }

    static var appToken: String? {
        get { Keychain.read(tokenKey) }
        set {
            if let newValue, !newValue.isEmpty { Keychain.write(tokenKey, newValue) } else { Keychain.delete(tokenKey) }
        }
    }

    static var isConfigured: Bool {
        serverURL != nil && !(appToken ?? "").isEmpty
    }

    /// Apple Developer Program（有料）の機能（通知・スクリーンタイム）を使えるか。
    /// 無料の Apple ID でビルドしたとき（Config の KENKO_EDITION = Free）は使えない
    static var hasPaidFeatures: Bool {
        (Bundle.main.object(forInfoDictionaryKey: "KenkoEdition") as? String) != "Free"
    }
}

/// キーチェーン（端末の中の、パスワードなどを安全に保存する場所）
enum Keychain {
    private static var service: String { Bundle.main.bundleIdentifier ?? "KenkoKanri" }

    private static func baseQuery(_ key: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
        ]
    }

    static func read(_ key: String) -> String? {
        var query = baseQuery(key)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func write(_ key: String, _ value: String) {
        delete(key)
        var query = baseQuery(key)
        query[kSecValueData as String] = Data(value.utf8)
        // 画面がロックされていても、一度ロックを解除したあとなら読める（バックグラウンドで送るため）
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(query as CFDictionary, nil)
    }

    static func delete(_ key: String) {
        SecItemDelete(baseQuery(key) as CFDictionary)
    }
}

extension Date {
    /// サーバーとのやり取りに使う、UTC のエポックミリ秒
    var epochMs: Int64 { Int64((timeIntervalSince1970 * 1000).rounded()) }

    init(epochMs: Double) {
        self.init(timeIntervalSince1970: epochMs / 1000)
    }
}
