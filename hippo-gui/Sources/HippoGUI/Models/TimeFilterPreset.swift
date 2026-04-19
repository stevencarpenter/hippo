import Foundation

enum TimeFilterPreset: String, CaseIterable, Identifiable, Sendable {
    case last24Hours
    case last7Days
    case all

    var id: String { rawValue }

    var title: String {
        switch self {
        case .last24Hours:
            "Last 24 h"
        case .last7Days:
            "Last 7 days"
        case .all:
            "All"
        }
    }

    var sinceMs: Int? {
        let now = Date()
        switch self {
        case .last24Hours:
            return Int(now.addingTimeInterval(-86_400).timeIntervalSince1970 * 1000)
        case .last7Days:
            return Int(now.addingTimeInterval(-604_800).timeIntervalSince1970 * 1000)
        case .all:
            return nil
        }
    }
}
