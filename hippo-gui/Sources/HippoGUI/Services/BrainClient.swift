import Foundation

enum BrainClientError: Error, LocalizedError, Equatable, Sendable {
    case invalidURL(String)
    case invalidResponse
    case encodingError(String)
    case networkError(String)
    case decodingError(String)
    case serverError(statusCode: Int, message: String)
    case notConfigured

    var errorDescription: String? {
        switch self {
        case .invalidURL(let value):
            return "Invalid URL: \(value)"
        case .invalidResponse:
            return "The server returned an invalid response."
        case .encodingError(let message):
            return "Encoding error: \(message)"
        case .networkError(let message):
            return "Network error: \(message)"
        case .decodingError(let message):
            return "Decoding error: \(message)"
        case .serverError(let statusCode, let message):
            return "Server error (HTTP \(statusCode)): \(message)"
        case .notConfigured:
            return "Brain client is not configured."
        }
    }
}

protocol BrainClientProtocol: Sendable {
    func listKnowledge(limit: Int, offset: Int, nodeType: String?, sinceMs: Int?) async throws(BrainClientError) -> KnowledgeListResponse
    func getKnowledge(id: Int) async throws(BrainClientError) -> KnowledgeNode
    func listEvents(limit: Int, offset: Int, sessionId: Int?, sinceMs: Int?, project: String?) async throws(BrainClientError) -> EventListResponse
    func listSessions(limit: Int, offset: Int, sinceMs: Int?) async throws(BrainClientError) -> SessionListResponse
    func ask(question: String, limit: Int) async throws(BrainClientError) -> AskResponse
    func query(_ text: String, limit: Int) async throws(BrainClientError) -> QueryResponse
    func health() async throws(BrainClientError) -> HealthResponse
}

actor BrainClient: BrainClientProtocol {
    private let baseURL: URL
    private let session: URLSession
    private let decoder = JSONDecoder()
    private let encoder = JSONEncoder()

    init(port: Int? = nil, configClient: ConfigClient = ConfigClient(), session: URLSession? = nil) {
        let resolvedPort = port ?? configClient.loadPort()
        guard let url = URL(string: "http://localhost:\(resolvedPort)") else {
            preconditionFailure("Failed to build base URL for port \(resolvedPort) — should be impossible for a numeric localhost URL")
        }
        self.baseURL = url
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = configClient.loadQueryTimeout()
        self.session = session ?? URLSession(configuration: config)
    }

    func listKnowledge(limit: Int = 20, offset: Int = 0, nodeType: String? = nil, sinceMs: Int? = nil) async throws(BrainClientError) -> KnowledgeListResponse {
        guard var components = URLComponents(url: baseURL.appending(path: "knowledge"), resolvingAgainstBaseURL: false) else {
            throw BrainClientError.invalidURL("\(baseURL.absoluteString)/knowledge")
        }
        var queryItems = [
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "offset", value: String(offset))
        ]
        if let nodeType = nodeType {
            queryItems.append(URLQueryItem(name: "node_type", value: nodeType))
        }
        if let sinceMs {
            queryItems.append(URLQueryItem(name: "since_ms", value: String(sinceMs)))
        }
        components.queryItems = queryItems

        guard let url = components.url else {
            throw BrainClientError.invalidURL("\(baseURL.absoluteString)/knowledge")
        }

        return try await get(url, as: KnowledgeListResponse.self)
    }

    func getKnowledge(id: Int) async throws(BrainClientError) -> KnowledgeNode {
        try await get(baseURL.appending(path: "knowledge/\(id)"), as: KnowledgeNode.self)
    }

    func listEvents(
        limit: Int = 20,
        offset: Int = 0,
        sessionId: Int? = nil,
        sinceMs: Int? = nil,
        project: String? = nil
    ) async throws(BrainClientError) -> EventListResponse {
        guard var components = URLComponents(url: baseURL.appending(path: "events"), resolvingAgainstBaseURL: false) else {
            throw BrainClientError.invalidURL("\(baseURL.absoluteString)/events")
        }
        var queryItems = [
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "offset", value: String(offset))
        ]
        if let sessionId = sessionId {
            queryItems.append(URLQueryItem(name: "session_id", value: String(sessionId)))
        }
        if let sinceMs {
            queryItems.append(URLQueryItem(name: "since_ms", value: String(sinceMs)))
        }
        if let project, !project.isEmpty {
            queryItems.append(URLQueryItem(name: "project", value: project))
        }
        components.queryItems = queryItems

        guard let url = components.url else {
            throw BrainClientError.invalidURL("\(baseURL.absoluteString)/events")
        }

        return try await get(url, as: EventListResponse.self)
    }

    func listSessions(limit: Int = 20, offset: Int = 0, sinceMs: Int? = nil) async throws(BrainClientError) -> SessionListResponse {
        var queryItems = [
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "offset", value: String(offset))
        ]
        if let sinceMs {
            queryItems.append(URLQueryItem(name: "since_ms", value: String(sinceMs)))
        }

        guard var components = URLComponents(url: baseURL.appending(path: "sessions"), resolvingAgainstBaseURL: false) else {
            throw BrainClientError.invalidURL("\(baseURL.absoluteString)/sessions")
        }
        components.queryItems = queryItems

        guard let url = components.url else {
            throw BrainClientError.invalidURL("\(baseURL.absoluteString)/sessions")
        }

        return try await get(url, as: SessionListResponse.self)
    }

    func ask(question: String, limit: Int = 10) async throws(BrainClientError) -> AskResponse {
        struct AskRequest: Encodable, Sendable {
            let question: String
            let limit: Int
        }

        return try await post(path: "ask", body: AskRequest(question: question, limit: limit), as: AskResponse.self)
    }

    func query(_ text: String, limit: Int = 10) async throws(BrainClientError) -> QueryResponse {
        struct QueryRequest: Encodable, Sendable {
            let text: String
            let limit: Int
        }

        return try await post(path: "query", body: QueryRequest(text: text, limit: limit), as: QueryResponse.self)
    }

    func health() async throws(BrainClientError) -> HealthResponse {
        try await get(baseURL.appending(path: "health"), as: HealthResponse.self)
    }

    private func get<T: Decodable>(_ url: URL, as type: T.Type) async throws(BrainClientError) -> T {
        let request = URLRequest(url: url)
        return try await execute(request, as: type)
    }

    private func post<Body: Encodable, T: Decodable>(path: String, body: Body, as type: T.Type) async throws(BrainClientError) -> T {
        let url = baseURL.appending(path: path)
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        do {
            request.httpBody = try encoder.encode(body)
        } catch {
            throw BrainClientError.encodingError(error.localizedDescription)
        }

        return try await execute(request, as: type)
    }

    private func execute<T: Decodable>(_ request: URLRequest, as type: T.Type) async throws(BrainClientError) -> T {
        do {
            let (data, response) = try await session.data(for: request)
            try validateResponse(response, data: data)

            do {
                return try decoder.decode(T.self, from: data)
            } catch {
                throw BrainClientError.decodingError(error.localizedDescription)
            }
        } catch let error as BrainClientError {
            throw error
        } catch {
            throw BrainClientError.networkError(error.localizedDescription)
        }
    }

    private func validateResponse(_ response: URLResponse, data: Data) throws(BrainClientError) {
        guard let httpResponse = response as? HTTPURLResponse else {
            throw BrainClientError.invalidResponse
        }

        guard (200...299).contains(httpResponse.statusCode) else {
            let message = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
            throw BrainClientError.serverError(
                statusCode: httpResponse.statusCode,
                message: message?.isEmpty == false ? message! : "Request failed"
            )
        }
    }
}
