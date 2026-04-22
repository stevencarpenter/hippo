import Foundation

struct KnowledgeNode: Identifiable, Codable, Hashable, Sendable {
    let id: Int
    let uuid: String
    let content: String
    let embedText: String?
    let nodeType: String
    let outcome: String?
    let tags: [String]
    let createdAt: Int
    let relatedEntities: [RelatedKnowledgeEntity]
    let relatedEvents: [RelatedKnowledgeEvent]

    enum CodingKeys: String, CodingKey {
        case id, uuid, content
        case embedText = "embed_text"
        case nodeType = "node_type"
        case outcome, tags
        case createdAt = "created_at"
        case relatedEntities = "related_entities"
        case relatedEvents = "related_events"
    }

    init(
        id: Int,
        uuid: String,
        content: String,
        embedText: String? = nil,
        nodeType: String,
        outcome: String? = nil,
        tags: [String] = [],
        createdAt: Int,
        relatedEntities: [RelatedKnowledgeEntity] = [],
        relatedEvents: [RelatedKnowledgeEvent] = []
    ) {
        self.id = id
        self.uuid = uuid
        self.content = content
        self.embedText = embedText
        self.nodeType = nodeType
        self.outcome = outcome
        self.tags = tags
        self.createdAt = createdAt
        self.relatedEntities = relatedEntities
        self.relatedEvents = relatedEvents
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(Int.self, forKey: .id)
        uuid = try container.decode(String.self, forKey: .uuid)
        content = try container.decode(String.self, forKey: .content)
        embedText = try container.decodeIfPresent(String.self, forKey: .embedText)
        nodeType = try container.decode(String.self, forKey: .nodeType)
        outcome = try container.decodeIfPresent(String.self, forKey: .outcome)
        createdAt = try container.decode(Int.self, forKey: .createdAt)
        tags = (try? container.decode([String].self, forKey: .tags)) ?? []
        relatedEntities = (try? container.decode([RelatedKnowledgeEntity].self, forKey: .relatedEntities)) ?? []
        relatedEvents = (try? container.decode([RelatedKnowledgeEvent].self, forKey: .relatedEvents)) ?? []
    }
}

struct RelatedKnowledgeEntity: Codable, Hashable, Sendable, Identifiable {
    let id: Int
    let name: String
    let type: String
}

struct RelatedKnowledgeEvent: Codable, Hashable, Sendable, Identifiable {
    let id: Int
    let command: String
}

extension KnowledgeNode {
    var displaySummary: String {
        guard let data = content.data(using: .utf8),
            let dict = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let summary = dict["summary"] as? String, !summary.isEmpty
        else {
            return content
        }
        return summary
    }
}

struct KnowledgeListResponse: Codable, Sendable {
    let nodes: [KnowledgeNode]
    let total: Int
}
