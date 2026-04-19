#if DEBUG
import Foundation

/// A lightweight stand-in for ``BrainClientProtocol`` used exclusively in SwiftUI ``#Preview`` blocks.
///
/// Unlike ``MockBrainClient`` (in `HippoGUITestHelpers`), this type lives inside `HippoGUIKit` so
/// that view preview code can reference it without creating a circular dependency.  It has no request
/// recording or sequence stepping — those features belong in the test target.
struct PreviewBrainClient: BrainClientProtocol {
    var knowledgeResponse: Result<KnowledgeListResponse, BrainClientError>
    var knowledgeDetails: [Int: Result<KnowledgeNode, BrainClientError>]
    var eventResponse: Result<EventListResponse, BrainClientError>
    var sessionResponse: Result<SessionListResponse, BrainClientError>
    var askResponse: Result<AskResponse, BrainClientError>
    var queryResponse: Result<QueryResponse, BrainClientError>
    var healthResponse: Result<HealthResponse, BrainClientError>

    init(
        knowledgeResponse: Result<KnowledgeListResponse, BrainClientError> = .success(.init(nodes: [], total: 0)),
        knowledgeDetails: [Int: Result<KnowledgeNode, BrainClientError>] = [:],
        eventResponse: Result<EventListResponse, BrainClientError> = .success(.init(events: [], total: 0)),
        sessionResponse: Result<SessionListResponse, BrainClientError> = .success(.init(sessions: [], total: 0)),
        askResponse: Result<AskResponse, BrainClientError> = .success(.init(answer: nil, sources: [], model: nil, error: nil, degraded: nil, stage: nil)),
        queryResponse: Result<QueryResponse, BrainClientError> = .success(.init(mode: .search)),
        healthResponse: Result<HealthResponse, BrainClientError> = .success(.preview)
    ) {
        self.knowledgeResponse = knowledgeResponse
        self.knowledgeDetails = knowledgeDetails
        self.eventResponse = eventResponse
        self.sessionResponse = sessionResponse
        self.askResponse = askResponse
        self.queryResponse = queryResponse
        self.healthResponse = healthResponse
    }

    func listKnowledge(limit: Int, offset: Int, nodeType: String?, sinceMs: Int?) async throws(BrainClientError) -> KnowledgeListResponse {
        switch knowledgeResponse {
        case .success(let r): return r
        case .failure(let e): throw e
        }
    }

    func getKnowledge(id: Int) async throws(BrainClientError) -> KnowledgeNode {
        if let detail = knowledgeDetails[id] {
            return try detail.get()
        }
        switch knowledgeResponse {
        case .success(let r):
            if let node = r.nodes.first(where: { $0.id == id }) { return node }
            throw BrainClientError.serverError(statusCode: 404, message: "Knowledge node not found")
        case .failure(let e):
            throw e
        }
    }

    func listEvents(limit: Int, offset: Int, sessionId: Int?, sinceMs: Int?, project: String?) async throws(BrainClientError) -> EventListResponse {
        switch eventResponse {
        case .success(let r): return r
        case .failure(let e): throw e
        }
    }

    func listSessions(limit: Int, offset: Int, sinceMs: Int?) async throws(BrainClientError) -> SessionListResponse {
        switch sessionResponse {
        case .success(let r): return r
        case .failure(let e): throw e
        }
    }

    func ask(question: String, limit: Int) async throws(BrainClientError) -> AskResponse {
        switch askResponse {
        case .success(let r): return r
        case .failure(let e): throw e
        }
    }

    func query(_ text: String, limit: Int) async throws(BrainClientError) -> QueryResponse {
        switch queryResponse {
        case .success(let r): return r
        case .failure(let e): throw e
        }
    }

    func health() async throws(BrainClientError) -> HealthResponse {
        switch healthResponse {
        case .success(let r): return r
        case .failure(let e): throw e
        }
    }
}
#endif
