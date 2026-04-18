import Foundation

struct KnowledgeNode: Identifiable, Codable, Hashable {
    let id: Int
    let uuid: String
    let content: String
    let nodeType: String
    let outcome: String?
    let tags: [String]
    let createdAt: Int

    enum CodingKeys: String, CodingKey {
        case id, uuid, content
        case nodeType = "node_type"
        case outcome, tags
        case createdAt = "created_at"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(Int.self, forKey: .id)
        uuid = try container.decode(String.self, forKey: .uuid)
        content = try container.decode(String.self, forKey: .content)
        nodeType = try container.decode(String.self, forKey: .nodeType)
        outcome = try container.decodeIfPresent(String.self, forKey: .outcome)
        createdAt = try container.decode(Int.self, forKey: .createdAt)
        tags = (try? container.decode([String].self, forKey: .tags)) ?? []
    }
}

extension KnowledgeNode {
    var displaySummary: String {
        guard let data = content.data(using: .utf8),
              let dict = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let summary = dict["summary"] as? String, !summary.isEmpty else {
            return content
        }
        return summary
    }
}

struct KnowledgeListResponse: Codable {
    let nodes: [KnowledgeNode]
    let total: Int
}
