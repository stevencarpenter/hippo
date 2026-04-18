import Foundation

enum BrainClientError: Error, LocalizedError {
    case invalidURL
    case networkError(Error)
    case decodingError(Error)
    case serverError(String)

    var errorDescription: String? {
        switch self {
        case .invalidURL:
            return "Invalid URL"
        case .networkError(let error):
            return "Network error: \(error.localizedDescription)"
        case .decodingError(let error):
            return "Decoding error: \(error.localizedDescription)"
        case .serverError(let message):
            return "Server error: \(message)"
        }
    }
}

actor BrainClient {
    private let baseURL = "http://localhost:8765"
    private let session: URLSession

    init() {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        self.session = URLSession(configuration: config)
    }

    func listKnowledge(limit: Int = 20, offset: Int = 0, nodeType: String? = nil) async throws -> KnowledgeListResponse {
        var components = URLComponents(string: "\(baseURL)/knowledge")!
        var queryItems = [
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "offset", value: String(offset))
        ]
        if let nodeType = nodeType {
            queryItems.append(URLQueryItem(name: "node_type", value: nodeType))
        }
        components.queryItems = queryItems

        guard let url = components.url else {
            throw BrainClientError.invalidURL
        }

        let (data, response) = try await session.data(from: url)
        try validateResponse(response)

        do {
            let decoder = JSONDecoder()
            return try decoder.decode(KnowledgeListResponse.self, from: data)
        } catch {
            throw BrainClientError.decodingError(error)
        }
    }

    func getKnowledge(id: Int) async throws -> KnowledgeNode {
        guard let url = URL(string: "\(baseURL)/knowledge/\(id)") else {
            throw BrainClientError.invalidURL
        }

        let (data, response) = try await session.data(from: url)
        try validateResponse(response)

        do {
            let decoder = JSONDecoder()
            return try decoder.decode(KnowledgeNode.self, from: data)
        } catch {
            throw BrainClientError.decodingError(error)
        }
    }

    func listEvents(limit: Int = 20, offset: Int = 0, sessionId: Int? = nil) async throws -> EventListResponse {
        var components = URLComponents(string: "\(baseURL)/events")!
        var queryItems = [
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "offset", value: String(offset))
        ]
        if let sessionId = sessionId {
            queryItems.append(URLQueryItem(name: "session_id", value: String(sessionId)))
        }
        components.queryItems = queryItems

        guard let url = components.url else {
            throw BrainClientError.invalidURL
        }

        let (data, response) = try await session.data(from: url)
        try validateResponse(response)

        do {
            let decoder = JSONDecoder()
            return try decoder.decode(EventListResponse.self, from: data)
        } catch {
            throw BrainClientError.decodingError(error)
        }
    }

    func listSessions(limit: Int = 20, offset: Int = 0) async throws -> SessionListResponse {
        var components = URLComponents(string: "\(baseURL)/sessions")!
        components.queryItems = [
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "offset", value: String(offset))
        ]

        guard let url = components.url else {
            throw BrainClientError.invalidURL
        }

        let (data, response) = try await session.data(from: url)
        try validateResponse(response)

        do {
            let decoder = JSONDecoder()
            return try decoder.decode(SessionListResponse.self, from: data)
        } catch {
            throw BrainClientError.decodingError(error)
        }
    }

    func ask(question: String, limit: Int = 10) async throws -> AskResponse {
        guard let url = URL(string: "\(baseURL)/ask") else {
            throw BrainClientError.invalidURL
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let body = ["question": question, "limit": limit]
        request.httpBody = try JSONEncoder().encode(body)

        let (data, response) = try await session.data(for: request)
        try validateResponse(response)

        do {
            let decoder = JSONDecoder()
            return try decoder.decode(AskResponse.self, from: data)
        } catch {
            throw BrainClientError.decodingError(error)
        }
    }

    func health() async throws -> Bool {
        guard let url = URL(string: "\(baseURL)/health") else {
            throw BrainClientError.invalidURL
        }

        let (data, response) = try await session.data(from: url)
        try validateResponse(response)

        do {
            let decoder = JSONDecoder()
            let result = try decoder.decode([String: Bool].self, from: data)
            return result["ok"] ?? false
        } catch {
            return false
        }
    }

    private func validateResponse(_ response: URLResponse) throws {
        guard let httpResponse = response as? HTTPURLResponse else {
            return
        }

        guard (200...299).contains(httpResponse.statusCode) else {
            throw BrainClientError.serverError("HTTP \(httpResponse.statusCode)")
        }
    }
}