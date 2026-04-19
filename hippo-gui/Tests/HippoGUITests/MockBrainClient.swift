import Foundation
@testable import HippoGUIKit

actor MockBrainClient: BrainClientProtocol {
    var knowledgeResponse: Result<KnowledgeListResponse, BrainClientError>
    var knowledgeResponsesSequence: [Result<KnowledgeListResponse, BrainClientError>]
    var knowledgeDetails: [Int: Result<KnowledgeNode, BrainClientError>]
    var eventResponse: Result<EventListResponse, BrainClientError>
    var eventResponsesSequence: [Result<EventListResponse, BrainClientError>]
    var sessionResponse: Result<SessionListResponse, BrainClientError>
    var sessionResponsesSequence: [Result<SessionListResponse, BrainClientError>]
    var askResponse: Result<AskResponse, BrainClientError>
    var queryResponse: Result<QueryResponse, BrainClientError>
    var healthResponse: Result<HealthResponse, BrainClientError>

    // swiftlint:disable large_tuple
    // Test-helper request snapshots. Tuples (rather than structs) keep the
    // call sites in MockBrainClient compact and the field accessors (.limit,
    // .offset, etc.) read identically in assertions.
    private(set) var lastKnowledgeRequest: (limit: Int, offset: Int, nodeType: String?, sinceMs: Int?)?
    private(set) var lastEventRequest: (limit: Int, offset: Int, sessionId: Int?, sinceMs: Int?, project: String?)?
    private(set) var lastSessionRequest: (limit: Int, offset: Int, sinceMs: Int?)?
    // swiftlint:enable large_tuple
    private(set) var lastAskRequest: (question: String, limit: Int)?
    private(set) var lastQueryRequest: (text: String, limit: Int)?

    init(
        knowledgeResponse: Result<KnowledgeListResponse, BrainClientError> = .success(.init(nodes: [], total: 0)),
        knowledgeResponsesSequence: [Result<KnowledgeListResponse, BrainClientError>] = [],
        knowledgeDetails: [Int: Result<KnowledgeNode, BrainClientError>] = [:],
        eventResponse: Result<EventListResponse, BrainClientError> = .success(.init(events: [], total: 0)),
        eventResponsesSequence: [Result<EventListResponse, BrainClientError>] = [],
        sessionResponse: Result<SessionListResponse, BrainClientError> = .success(.init(sessions: [], total: 0)),
        sessionResponsesSequence: [Result<SessionListResponse, BrainClientError>] = [],
        askResponse: Result<AskResponse, BrainClientError> = .success(.init(answer: nil, sources: [], model: nil, error: nil, degraded: nil, stage: nil)),
        queryResponse: Result<QueryResponse, BrainClientError> = .success(.init(mode: .search)),
        healthResponse: Result<HealthResponse, BrainClientError> = .success(.preview)
    ) {
        self.knowledgeResponse = knowledgeResponse
        self.knowledgeResponsesSequence = knowledgeResponsesSequence
        self.knowledgeDetails = knowledgeDetails
        self.eventResponse = eventResponse
        self.eventResponsesSequence = eventResponsesSequence
        self.sessionResponse = sessionResponse
        self.sessionResponsesSequence = sessionResponsesSequence
        self.askResponse = askResponse
        self.queryResponse = queryResponse
        self.healthResponse = healthResponse
    }

    func listKnowledge(limit: Int, offset: Int, nodeType: String?, sinceMs: Int?) async throws(BrainClientError) -> KnowledgeListResponse {
        lastKnowledgeRequest = (limit, offset, nodeType, sinceMs)
        if !knowledgeResponsesSequence.isEmpty {
            switch knowledgeResponsesSequence.removeFirst() {
            case .success(let response):
                return response
            case .failure(let error):
                throw error
            }
        }
        switch knowledgeResponse {
        case .success(let response):
            return response
        case .failure(let error):
            throw error
        }
    }

    func getKnowledge(id: Int) async throws(BrainClientError) -> KnowledgeNode {
        if let detail = knowledgeDetails[id] {
            return try detail.get()
        }

        let response: KnowledgeListResponse
        switch knowledgeResponse {
        case .success(let value):
            response = value
        case .failure(let error):
            throw error
        }

        if let node = response.nodes.first(where: { $0.id == id }) {
            return node
        }

        throw BrainClientError.serverError(statusCode: 404, message: "Knowledge node not found")
    }

    func listEvents(limit: Int, offset: Int, sessionId: Int?, sinceMs: Int?, project: String?) async throws(BrainClientError) -> EventListResponse {
        lastEventRequest = (limit, offset, sessionId, sinceMs, project)
        if !eventResponsesSequence.isEmpty {
            switch eventResponsesSequence.removeFirst() {
            case .success(let response):
                return response
            case .failure(let error):
                throw error
            }
        }
        switch eventResponse {
        case .success(let response):
            return response
        case .failure(let error):
            throw error
        }
    }

    func listSessions(limit: Int, offset: Int, sinceMs: Int?) async throws(BrainClientError) -> SessionListResponse {
        lastSessionRequest = (limit, offset, sinceMs)
        if !sessionResponsesSequence.isEmpty {
            switch sessionResponsesSequence.removeFirst() {
            case .success(let response):
                return response
            case .failure(let error):
                throw error
            }
        }
        switch sessionResponse {
        case .success(let response):
            return response
        case .failure(let error):
            throw error
        }
    }

    func ask(question: String, limit: Int) async throws(BrainClientError) -> AskResponse {
        lastAskRequest = (question, limit)
        switch askResponse {
        case .success(let response):
            return response
        case .failure(let error):
            throw error
        }
    }

    func query(_ text: String, limit: Int) async throws(BrainClientError) -> QueryResponse {
        lastQueryRequest = (text, limit)
        switch queryResponse {
        case .success(let response):
            return response
        case .failure(let error):
            throw error
        }
    }

    func health() async throws(BrainClientError) -> HealthResponse {
        switch healthResponse {
        case .success(let response):
            return response
        case .failure(let error):
            throw error
        }
    }
}
