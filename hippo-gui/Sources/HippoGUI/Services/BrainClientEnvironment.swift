import SwiftUI

private struct BrainClientKey: EnvironmentKey {
    static let defaultValue: any BrainClientProtocol = UnconfiguredBrainClient()
}

extension EnvironmentValues {
    var brainClient: any BrainClientProtocol {
        get { self[BrainClientKey.self] }
        set { self[BrainClientKey.self] = newValue }
    }
}

extension View {
    func brainClient(_ brainClient: any BrainClientProtocol) -> some View {
        environment(\.brainClient, brainClient)
    }
}

private actor UnconfiguredBrainClient: BrainClientProtocol {
    func listKnowledge(
        limit: Int, offset: Int, nodeType: String?, sinceMs: Int?
    ) async throws(BrainClientError) -> KnowledgeListResponse {
        throw .notConfigured
    }

    func getKnowledge(id: Int) async throws(BrainClientError) -> KnowledgeNode {
        throw .notConfigured
    }

    func listEvents(
        limit: Int, offset: Int, sessionId: Int?, sinceMs: Int?, project: String?
    ) async throws(BrainClientError) -> EventListResponse {
        throw .notConfigured
    }

    func listSessions(limit: Int, offset: Int, sinceMs: Int?) async throws(BrainClientError) -> SessionListResponse {
        throw .notConfigured
    }

    func ask(question: String, limit: Int) async throws(BrainClientError) -> AskResponse {
        throw .notConfigured
    }

    func query(_ text: String, limit: Int) async throws(BrainClientError) -> QueryResponse {
        throw .notConfigured
    }

    func health() async throws(BrainClientError) -> HealthResponse {
        throw .notConfigured
    }
}
