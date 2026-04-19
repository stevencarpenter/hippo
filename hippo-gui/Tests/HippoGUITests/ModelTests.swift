import Foundation
import Testing
@testable import HippoGUIKit

struct ModelTests {

    // MARK: - HealthResponse computed properties

    @Test
    func healthResponseBrainReachableForOkStatus() {
        let health = makeHealth(status: "ok")
        #expect(health.brainReachable)
    }

    @Test
    func healthResponseBrainReachableForDegradedStatus() {
        let health = makeHealth(status: "degraded")
        #expect(health.brainReachable)
    }

    @Test
    func healthResponseBrainNotReachableForOtherStatus() {
        #expect(!makeHealth(status: "error").brainReachable)
        #expect(!makeHealth(status: "unknown").brainReachable)
        #expect(!makeHealth(status: "").brainReachable)
    }

    @Test
    func healthResponseTotalPendingQueueDepth() {
        let health = makeHealth(queueDepth: 3, claudeQueueDepth: 2, browserQueueDepth: 1, workflowQueueDepth: 4)
        #expect(health.totalPendingQueueDepth == 10)
    }

    @Test
    func healthResponseTotalFailedQueueDepth() {
        let health = makeHealth(queueFailed: 1, claudeQueueFailed: 2, browserQueueFailed: 3, workflowQueueFailed: 4)
        #expect(health.totalFailedQueueDepth == 10)
    }

    @Test
    func healthResponseZeroQueuesAreZero() {
        let health = makeHealth()
        #expect(health.totalPendingQueueDepth == 0)
        #expect(health.totalFailedQueueDepth == 0)
    }

    // MARK: - KnowledgeNode.displaySummary

    @Test
    func knowledgeNodeDisplaySummaryExtractsFromJSON() {
        let node = KnowledgeNode(id: 1, uuid: "u1", content: "{\"summary\":\"Refactored auth\"}", nodeType: "observation", createdAt: 0)
        #expect(node.displaySummary == "Refactored auth")
    }

    @Test
    func knowledgeNodeDisplaySummaryFallsBackForNonJSON() {
        let node = KnowledgeNode(id: 1, uuid: "u1", content: "plain text content", nodeType: "observation", createdAt: 0)
        #expect(node.displaySummary == "plain text content")
    }

    @Test
    func knowledgeNodeDisplaySummaryFallsBackWhenNoSummaryKey() {
        let content = "{\"key_decisions\":[\"use async\"]}"
        let node = KnowledgeNode(id: 1, uuid: "u1", content: content, nodeType: "observation", createdAt: 0)
        #expect(node.displaySummary == content)
    }

    @Test
    func knowledgeNodeDisplaySummaryFallsBackForEmptySummary() {
        let content = "{\"summary\":\"\"}"
        let node = KnowledgeNode(id: 1, uuid: "u1", content: content, nodeType: "observation", createdAt: 0)
        #expect(node.displaySummary == content)
    }

    // MARK: - QueryResponse.isEmpty

    @Test
    func queryResponseIsEmptyWhenNoResultsEventsOrNodes() {
        #expect(QueryResponse(mode: .search).isEmpty)
        #expect(QueryResponse(mode: .ask).isEmpty)
    }

    @Test
    func queryResponseIsNotEmptyWithResults() {
        let result = SemanticQueryResult(summary: "Found something")
        #expect(!QueryResponse(mode: .semantic, results: [result]).isEmpty)
    }

    @Test
    func queryResponseIsNotEmptyWithEvents() {
        let event = LexicalQueryEvent(eventId: 1, command: "swift test", cwd: "/tmp", timestamp: 0)
        #expect(!QueryResponse(mode: .lexical, events: [event]).isEmpty)
    }

    @Test
    func queryResponseIsNotEmptyWithNodes() {
        let node = LexicalKnowledgeMatch(id: 1, uuid: "u1", content: "raw", embedText: nil)
        #expect(!QueryResponse(mode: .lexical, nodes: [node]).isEmpty)
    }

    // MARK: - QueryMode.isSearchMode

    @Test
    func queryModeAskIsNotSearchMode() {
        #expect(!QueryMode.ask.isSearchMode)
    }

    @Test
    func queryModeSearchIsSearchMode() {
        #expect(QueryMode.search.isSearchMode)
        #expect(QueryMode.lexical.isSearchMode)
        #expect(QueryMode.semantic.isSearchMode)
    }

    // MARK: - SidebarSection

    @Test
    func sidebarSectionTitles() {
        #expect(SidebarSection.query.title == "Query")
        #expect(SidebarSection.knowledge.title == "Knowledge")
        #expect(SidebarSection.events.title == "Events")
        #expect(SidebarSection.status.title == "Status")
    }

    @Test
    func sidebarSectionSystemImages() {
        #expect(SidebarSection.query.systemImage == "questionmark.circle")
        #expect(SidebarSection.knowledge.systemImage == "brain")
        #expect(SidebarSection.events.systemImage == "terminal")
        #expect(SidebarSection.status.systemImage == "heart")
    }

    @Test
    func sidebarSectionIdMatchesRawValue() {
        for section in SidebarSection.allCases {
            #expect(section.id == section.rawValue)
        }
    }

    // MARK: - TimeFilterPreset

    @Test
    func timeFilterPresetTitles() {
        #expect(TimeFilterPreset.last24Hours.title == "Last 24 h")
        #expect(TimeFilterPreset.last7Days.title == "Last 7 days")
        #expect(TimeFilterPreset.all.title == "All")
    }

    @Test
    func timeFilterPresetAllHasNilSinceMs() {
        #expect(TimeFilterPreset.all.sinceMs == nil)
    }

    @Test
    func timeFilterPresetLast24HoursHasApproximateSinceMs() {
        let lowerBound = Int(Date().addingTimeInterval(-86_401).timeIntervalSince1970 * 1000)
        let sinceMs = TimeFilterPreset.last24Hours.sinceMs
        let upperBound = Int(Date().addingTimeInterval(-86_399).timeIntervalSince1970 * 1000)

        #expect(sinceMs != nil)
        #expect((sinceMs ?? 0) >= lowerBound)
        #expect((sinceMs ?? 0) <= upperBound)
    }

    @Test
    func timeFilterPresetLast7DaysHasApproximateSinceMs() {
        let lowerBound = Int(Date().addingTimeInterval(-604_801).timeIntervalSince1970 * 1000)
        let sinceMs = TimeFilterPreset.last7Days.sinceMs
        let upperBound = Int(Date().addingTimeInterval(-604_799).timeIntervalSince1970 * 1000)

        #expect(sinceMs != nil)
        #expect((sinceMs ?? 0) >= lowerBound)
        #expect((sinceMs ?? 0) <= upperBound)
    }

    @Test
    func timeFilterPresetIdMatchesRawValue() {
        for preset in TimeFilterPreset.allCases {
            #expect(preset.id == preset.rawValue)
        }
    }

    // MARK: - BrainClientError.errorDescription

    @Test
    func brainClientErrorDescriptions() {
        #expect(BrainClientError.invalidURL("http://bad").localizedDescription == "Invalid URL: http://bad")
        #expect(BrainClientError.invalidResponse.localizedDescription == "The server returned an invalid response.")
        #expect(BrainClientError.encodingError("bad encoding").localizedDescription == "Encoding error: bad encoding")
        #expect(BrainClientError.networkError("timeout").localizedDescription == "Network error: timeout")
        #expect(BrainClientError.decodingError("bad json").localizedDescription == "Decoding error: bad json")
        #expect(BrainClientError.serverError(statusCode: 500, message: "Internal Server Error").localizedDescription == "Server error (HTTP 500): Internal Server Error")
        #expect(BrainClientError.notConfigured.localizedDescription == "Brain client is not configured.")
    }

    @Test
    func brainClientErrorEquality() {
        #expect(BrainClientError.notConfigured == BrainClientError.notConfigured)
        #expect(BrainClientError.networkError("x") == BrainClientError.networkError("x"))
        #expect(BrainClientError.networkError("x") != BrainClientError.networkError("y"))
        #expect(BrainClientError.serverError(statusCode: 404, message: "Not found") == BrainClientError.serverError(statusCode: 404, message: "Not found"))
    }

    // MARK: - Helpers

    private func makeHealth(
        status: String = "ok",
        queueDepth: Int = 0,
        queueFailed: Int = 0,
        claudeQueueDepth: Int = 0,
        claudeQueueFailed: Int = 0,
        browserQueueDepth: Int = 0,
        browserQueueFailed: Int = 0,
        workflowQueueDepth: Int = 0,
        workflowQueueFailed: Int = 0
    ) -> HealthResponse {
        HealthResponse(
            status: status,
            version: nil,
            lmstudioReachable: false,
            enrichmentRunning: false,
            dbReachable: true,
            queueDepth: queueDepth,
            queueFailed: queueFailed,
            claudeQueueDepth: claudeQueueDepth,
            claudeQueueFailed: claudeQueueFailed,
            browserQueueDepth: browserQueueDepth,
            browserQueueFailed: browserQueueFailed,
            workflowQueueDepth: workflowQueueDepth,
            workflowQueueFailed: workflowQueueFailed,
            enrichmentModel: nil,
            enrichmentModelPreferred: nil,
            queryInflight: nil,
            embedModelDrift: nil,
            lastSuccessAtMs: nil,
            lastError: nil,
            lastErrorAtMs: nil
        )
    }
}
