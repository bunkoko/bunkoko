import Foundation

enum APIError: LocalizedError {
    case notConfigured
    case http(status: Int, body: String)

    var errorDescription: String? {
        switch self {
        case .notConfigured:
            return "サーバーの URL と合言葉が設定されていません（設定画面で入力してください）"
        case let .http(status, body):
            switch status {
            case 401: return "合言葉が違います（設定画面の APP_TOKEN を確かめてください）"
            case 404: return "データがありません"
            default: return "サーバーとの通信に失敗しました（\(status)）\(body.prefix(200))"
            }
        }
    }
}

/// サーバーの API（/api/*）を呼ぶ
struct APIClient: Sendable {
    static let shared = APIClient()

    private func request(_ method: String, _ path: String, query: [String: String] = [:], body: Data? = nil) async throws -> Data {
        guard let base = AppConfig.serverURL, let token = AppConfig.appToken, !token.isEmpty else {
            throw APIError.notConfigured
        }
        var components = URLComponents(url: base.appending(path: path), resolvingAgainstBaseURL: false)
        if !query.isEmpty {
            components?.queryItems = query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        guard let url = components?.url else { throw APIError.notConfigured }

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 30
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            throw APIError.http(status: status, body: String(data: data, encoding: .utf8) ?? "")
        }
        return data
    }

    private func get<T: Decodable>(_ path: String, query: [String: String] = [:]) async throws -> T {
        try JSONDecoder().decode(T.self, from: try await request("GET", path, query: query))
    }

    private func send<Body: Encodable, T: Decodable>(_ method: String, _ path: String, _ body: Body) async throws -> T {
        let data = try JSONEncoder().encode(body)
        return try JSONDecoder().decode(T.self, from: try await request(method, path, body: data))
    }

    // MARK: - 送る

    func uploadHealth(_ batch: HealthBatch) async throws {
        let _: OKResponse = try await send("POST", "api/health/batch", batch)
    }

    func uploadLogs(_ batch: LogsBatch) async throws {
        let _: OKResponse = try await send("POST", "api/logs/batch", batch)
    }

    func registerAPNs(token: String) async throws {
        #if DEBUG
        let environment = "sandbox"
        #else
        let environment = "production"
        #endif
        let _: OKResponse = try await send("POST", "api/devices/apns", APNsRegistration(token: token, environment: environment))
    }

    func updateEstimate(mealID: String, estimate: MealEstimate) async throws {
        let _: OKResponse = try await send("PUT", "api/meals/\(mealID)/estimate", EstimateUpdate(estimate: estimate))
    }

    func saveSettings(_ settings: ServerSettings) async throws -> ServerSettings {
        let response: SettingsResponse = try await send("PUT", "api/settings", settings)
        return response.settings
    }

    // MARK: - 見る

    func home() async throws -> HomeResponse {
        try await get("api/home")
    }

    func days(from: String, to: String) async throws -> [DayView] {
        let response: DaysResponse = try await get("api/days", query: ["from": from, "to": to])
        return response.days
    }

    func timeline(date: String) async throws -> TimelineResponse {
        try await get("api/nights/\(date)/timeline")
    }

    func meals(from: String, to: String) async throws -> [Meal] {
        let response: MealsResponse = try await get("api/meals", query: ["from": from, "to": to])
        return response.meals
    }

    func settings() async throws -> ServerSettings {
        let response: SettingsResponse = try await get("api/settings")
        return response.settings
    }
}
