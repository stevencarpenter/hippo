import Foundation

struct AskResponse: Codable, Sendable {
    let answer: String?
    let sources: [AskSource]?
    let model: String?
    let error: String?
    let degraded: Bool?
    let stage: String?
}

struct AskSource: Codable, Identifiable, Hashable, Sendable {
    let id: UUID
    let sourceId: Int?
    let summary: String
    let score: Double?
    let cwd: String?
    let gitBranch: String?
    let timestamp: Int?
    let commandsRaw: String?
    let uuid: String?
    let linkedEventIds: [Int]

    enum CodingKeys: String, CodingKey {
        case sourceId = "id"
        case summary, score
        case cwd
        case gitBranch = "git_branch"
        case timestamp
        case commandsRaw = "commands_raw"
        case uuid
        case linkedEventIds = "linked_event_ids"
    }

    init(
        id: UUID = UUID(),
        sourceId: Int? = nil,
        summary: String,
        score: Double? = nil,
        cwd: String? = nil,
        gitBranch: String? = nil,
        timestamp: Int? = nil,
        commandsRaw: String? = nil,
        uuid: String? = nil,
        linkedEventIds: [Int] = []
    ) {
        self.id = id
        self.sourceId = sourceId
        self.summary = summary
        self.score = score
        self.cwd = cwd
        self.gitBranch = gitBranch
        self.timestamp = timestamp
        self.commandsRaw = commandsRaw
        self.uuid = uuid
        self.linkedEventIds = linkedEventIds
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = UUID()
        sourceId = try container.decodeIfPresent(Int.self, forKey: .sourceId)
        summary = try container.decode(String.self, forKey: .summary)
        score = try container.decodeIfPresent(Double.self, forKey: .score)
        cwd = try container.decodeIfPresent(String.self, forKey: .cwd)
        gitBranch = try container.decodeIfPresent(String.self, forKey: .gitBranch)
        timestamp = try container.decodeIfPresent(Int.self, forKey: .timestamp)
        commandsRaw = try container.decodeIfPresent(String.self, forKey: .commandsRaw)
        uuid = try container.decodeIfPresent(String.self, forKey: .uuid)
        linkedEventIds = (try? container.decode([Int].self, forKey: .linkedEventIds)) ?? []
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encodeIfPresent(sourceId, forKey: .sourceId)
        try container.encode(summary, forKey: .summary)
        try container.encodeIfPresent(score, forKey: .score)
        try container.encodeIfPresent(cwd, forKey: .cwd)
        try container.encodeIfPresent(gitBranch, forKey: .gitBranch)
        try container.encodeIfPresent(timestamp, forKey: .timestamp)
        try container.encodeIfPresent(commandsRaw, forKey: .commandsRaw)
        try container.encodeIfPresent(uuid, forKey: .uuid)
        try container.encode(linkedEventIds, forKey: .linkedEventIds)
    }
}
