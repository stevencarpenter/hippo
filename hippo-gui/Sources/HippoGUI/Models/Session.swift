import Foundation

struct Session: Identifiable, Codable, Hashable, Sendable {
    let id: Int
    let startTime: Int
    let hostname: String
    let shell: String
    let eventCount: Int

    enum CodingKeys: String, CodingKey {
        case id
        case startTime = "start_time"
        case hostname, shell
        case eventCount = "event_count"
    }
}

struct SessionListResponse: Codable, Sendable {
    let sessions: [Session]
    let total: Int
}
