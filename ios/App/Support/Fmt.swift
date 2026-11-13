import Foundation

/// 画面に出す数字や時刻の書き方
enum Fmt {
    /// 425 → "7時間5分"
    static func duration(_ minutes: Double?) -> String {
        guard let minutes, minutes.isFinite else { return "—" }
        let total = Int(minutes.rounded())
        let h = total / 60
        let m = total % 60
        if h == 0 { return "\(m)分" }
        return m == 0 ? "\(h)時間" : "\(h)時間\(m)分"
    }

    private static let timeFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ja_JP")
        formatter.dateFormat = "H:mm"
        return formatter
    }()

    /// エポックミリ秒 → "23:40"
    static func time(_ epochMs: Double?) -> String {
        guard let epochMs else { return "—" }
        return timeFormatter.string(from: Date(epochMs: epochMs))
    }

    static func time(_ date: Date) -> String {
        timeFormatter.string(from: date)
    }

    /// 1203 → "1,203ppm"
    static func number(_ value: Double?, digits: Int = 0, unit: String = "") -> String {
        guard let value, value.isFinite else { return "—" }
        return value.formatted(.number.precision(.fractionLength(0...digits)).locale(Locale(identifier: "ja_JP"))) + unit
    }

    /// 0.924 → "92%"
    static func percent(_ value: Double?) -> String {
        guard let value, value.isFinite else { return "—" }
        return "\(Int((value * 100).rounded()))%"
    }

    private static let dateParser: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }()

    private static let dateLabelFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ja_JP")
        formatter.dateFormat = "M/d（E）"
        return formatter
    }()

    /// "2026-11-21" → "11/21（土）"
    static func dateLabel(_ date: String) -> String {
        guard let parsed = dateParser.date(from: date) else { return date }
        return dateLabelFormatter.string(from: parsed)
    }

    static func dateString(_ date: Date) -> String {
        dateParser.string(from: date)
    }

    static func parseDate(_ date: String) -> Date? {
        dateParser.date(from: date)
    }
}
