import Foundation

struct Event: Identifiable, Codable, Hashable, Sendable {
    let id: Int
    let sessionId: Int
    let timestamp: Int
    let command: String
    let exitCode: Int?
    let durationMs: Int
    let cwd: String
    let gitBranch: String?

    enum CodingKeys: String, CodingKey {
        case id, sessionId = "session_id", timestamp, command
        case exitCode = "exit_code"
        case durationMs = "duration_ms"
        case cwd, gitBranch = "git_branch"
    }
}

struct EventListResponse: Codable, Sendable {
    let events: [Event]
    let total: Int
}