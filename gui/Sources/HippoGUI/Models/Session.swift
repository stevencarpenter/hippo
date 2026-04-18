import Foundation

struct Session: Identifiable, Codable {
    let id: Int
    let startTime: Int
    let hostname: String
    let shell: String
    let eventCount: Int

    enum CodingKeys: String, CodingKey {
        case id, startTime = "start_time"
        case hostname, shell, eventCount = "event_count"
    }
}

struct SessionListResponse: Codable {
    let sessions: [Session]
    let total: Int
}