import Foundation

struct AskResponse: Codable {
    let answer: String?
    let sources: [AskSource]?
    let model: String?
    let error: String?
}

struct AskSource: Codable, Identifiable {
    let id: UUID
    let sourceId: Int?
    let summary: String
    let score: Double?

    enum CodingKeys: String, CodingKey {
        case sourceId = "id"
        case summary, score
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = UUID()
        sourceId = try container.decodeIfPresent(Int.self, forKey: .sourceId)
        summary = try container.decode(String.self, forKey: .summary)
        score = try container.decodeIfPresent(Double.self, forKey: .score)
    }
}
