import Foundation

struct KnowledgeNode: Identifiable, Codable {
    let id: Int
    let uuid: String
    let content: String
    let nodeType: String
    let outcome: String?
    let tags: String?
    let createdAt: Int

    enum CodingKeys: String, CodingKey {
        case id, uuid, content
        case nodeType = "node_type"
        case outcome, tags
        case createdAt = "created_at"
    }
}

struct KnowledgeListResponse: Codable {
    let nodes: [KnowledgeNode]
    let total: Int
}