import Foundation

struct AskResponse: Codable {
    let answer: String?
    let sources: [AskSource]?
    let model: String?
    let error: String?
}

struct AskSource: Codable, Identifiable {
    let id: Int
    let summary: String
    let score: Double?
}