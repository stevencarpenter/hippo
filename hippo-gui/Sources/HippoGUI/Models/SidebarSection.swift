import Foundation

enum SidebarSection: String, CaseIterable, Identifiable, Sendable {
    case query
    case knowledge
    case events
    case status

    var id: String { rawValue }

    var title: String {
        switch self {
        case .query:
            "Query"
        case .knowledge:
            "Knowledge"
        case .events:
            "Events"
        case .status:
            "Status"
        }
    }

    var systemImage: String {
        switch self {
        case .query:
            "questionmark.circle"
        case .knowledge:
            "brain"
        case .events:
            "terminal"
        case .status:
            "heart"
        }
    }
}
