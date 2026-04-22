import Foundation

struct QueryResponse: Codable, Sendable {
    let mode: QueryMode
    let warning: String?
    let results: [SemanticQueryResult]
    let events: [LexicalQueryEvent]
    let nodes: [LexicalKnowledgeMatch]

    init(
        mode: QueryMode,
        warning: String? = nil,
        results: [SemanticQueryResult] = [],
        events: [LexicalQueryEvent] = [],
        nodes: [LexicalKnowledgeMatch] = []
    ) {
        self.mode = mode
        self.warning = warning
        self.results = results
        self.events = events
        self.nodes = nodes
    }

    enum CodingKeys: String, CodingKey {
        case mode
        case warning
        case results
        case events
        case nodes
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        mode = try container.decodeIfPresent(QueryMode.self, forKey: .mode) ?? .search
        warning = try container.decodeIfPresent(String.self, forKey: .warning)
        results = try container.decodeIfPresent([SemanticQueryResult].self, forKey: .results) ?? []
        events = try container.decodeIfPresent([LexicalQueryEvent].self, forKey: .events) ?? []
        nodes = try container.decodeIfPresent([LexicalKnowledgeMatch].self, forKey: .nodes) ?? []
    }

    var isEmpty: Bool {
        results.isEmpty && events.isEmpty && nodes.isEmpty
    }
}

struct SemanticQueryResult: Codable, Identifiable, Hashable, Sendable {
    let id = UUID()
    let score: Double?
    let summary: String
    let tags: [String]
    let keyDecisions: [String]
    let problemsEncountered: [String]
    let cwd: String
    let gitBranch: String?
    let sessionId: Int?
    let commandsRaw: String?
    let embedText: String?

    enum CodingKeys: String, CodingKey {
        case score
        case summary
        case tags
        case keyDecisions = "key_decisions"
        case problemsEncountered = "problems_encountered"
        case cwd
        case gitBranch = "git_branch"
        case sessionId = "session_id"
        case commandsRaw = "commands_raw"
        case embedText = "embed_text"
    }

    init(
        score: Double? = nil,
        summary: String,
        tags: [String] = [],
        keyDecisions: [String] = [],
        problemsEncountered: [String] = [],
        cwd: String = "",
        gitBranch: String? = nil,
        sessionId: Int? = nil,
        commandsRaw: String? = nil,
        embedText: String? = nil
    ) {
        self.score = score
        self.summary = summary
        self.tags = tags
        self.keyDecisions = keyDecisions
        self.problemsEncountered = problemsEncountered
        self.cwd = cwd
        self.gitBranch = gitBranch
        self.sessionId = sessionId
        self.commandsRaw = commandsRaw
        self.embedText = embedText
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        score = try container.decodeIfPresent(Double.self, forKey: .score)
        summary = try container.decodeIfPresent(String.self, forKey: .summary) ?? ""
        tags = Self.decodeStringArrayIfNeeded(from: container, forKey: .tags)
        keyDecisions = Self.decodeStringArrayIfNeeded(from: container, forKey: .keyDecisions)
        problemsEncountered = Self.decodeStringArrayIfNeeded(from: container, forKey: .problemsEncountered)
        cwd = try container.decodeIfPresent(String.self, forKey: .cwd) ?? ""
        gitBranch = try container.decodeIfPresent(String.self, forKey: .gitBranch)
        sessionId = try container.decodeIfPresent(Int.self, forKey: .sessionId)
        commandsRaw = try container.decodeIfPresent(String.self, forKey: .commandsRaw)
        embedText = try container.decodeIfPresent(String.self, forKey: .embedText)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encodeIfPresent(score, forKey: .score)
        try container.encode(summary, forKey: .summary)
        try container.encode(tags, forKey: .tags)
        try container.encode(keyDecisions, forKey: .keyDecisions)
        try container.encode(problemsEncountered, forKey: .problemsEncountered)
        try container.encode(cwd, forKey: .cwd)
        try container.encodeIfPresent(gitBranch, forKey: .gitBranch)
        try container.encodeIfPresent(sessionId, forKey: .sessionId)
        try container.encodeIfPresent(commandsRaw, forKey: .commandsRaw)
        try container.encodeIfPresent(embedText, forKey: .embedText)
    }

    private static func decodeStringArrayIfNeeded(
        from container: KeyedDecodingContainer<CodingKeys>,
        forKey key: CodingKeys
    ) -> [String] {
        if let strings = try? container.decode([String].self, forKey: key) {
            return strings
        }

        if let jsonString = try? container.decode(String.self, forKey: key),
            let data = jsonString.data(using: .utf8),
            let decoded = try? JSONDecoder().decode([String].self, from: data) {
            return decoded
        }

        return []
    }
}

struct LexicalQueryEvent: Codable, Identifiable, Hashable, Sendable {
    let eventId: Int
    let command: String
    let cwd: String
    let timestamp: Int

    enum CodingKeys: String, CodingKey {
        case eventId = "event_id"
        case command
        case cwd
        case timestamp
    }

    var id: Int { eventId }
}

struct LexicalKnowledgeMatch: Codable, Identifiable, Hashable, Sendable {
    let id: Int
    let uuid: String
    let content: String
    let embedText: String?

    enum CodingKeys: String, CodingKey {
        case id
        case uuid
        case content
        case embedText = "embed_text"
    }
}

enum QueryMode: String, Codable, CaseIterable, Identifiable, Sendable {
    case ask
    case search
    case lexical
    case semantic

    var id: String { rawValue }

    var isSearchMode: Bool {
        switch self {
        case .search, .lexical, .semantic:
            true
        case .ask:
            false
        }
    }
}
